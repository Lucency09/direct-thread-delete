#!/usr/bin/env python3
"""Local browser UI for listing and deleting persisted Codex threads."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from thread_admin import (
    ALL_SOURCE_KINDS,
    INTERACTIVE_SOURCE_KINDS,
    AppServer,
    ProtocolError,
    compact_row,
    load_threads,
    matches,
    resolve_codex_home,
    validate_uuid,
)


APP_NAME = "Codex 会话管理器"
MANAGER_VERSION = 7
PAGE_SIZES = {10, 20, 50, 100}
COORDINATOR_LEASE_SECONDS = 70
DELETE_COORDINATION_TIMEOUT_SECONDS = 180


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def project_info(cwd: Any) -> dict[str, str]:
    raw = str(cwd or "").strip()
    if not raw:
        return {"key": "__none__", "name": "无项目", "path": ""}
    normalized = os.path.normcase(os.path.normpath(raw))
    name = Path(raw.rstrip("\\/")).name or raw
    return {"key": normalized, "name": name, "path": raw}


class ThreadStore:
    def __init__(
        self,
        codex_bin: str,
        codex_home: str | None,
        protected_thread_id: str | None,
    ) -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.protected_thread_id = protected_thread_id
        self.lock = threading.Lock()

    def _load(self, all_sources: bool) -> list[dict[str, Any]]:
        source_kinds = ALL_SOURCE_KINDS if all_sources else INTERACTIVE_SOURCE_KINDS
        with AppServer(self.codex_bin, self.codex_home) as server:
            rows = load_threads(server, "all", source_kinds=source_kinds)
        rows.sort(
            key=lambda row: row.get("updatedAt") or row.get("createdAt") or 0,
            reverse=True,
        )
        return rows

    def preflight(self) -> int:
        """Prove the selected Codex home is readable before exposing the UI."""
        with self.lock:
            return len(self._load(all_sources=False))

    def page(
        self,
        page: int,
        page_size: int,
        state: str,
        project: str,
        search: str,
        all_sources: bool,
    ) -> dict[str, Any]:
        with self.lock:
            rows = self._load(all_sources)

        stats = {
            "total": len(rows),
            "active": sum(row.get("storageState") == "active" for row in rows),
            "archived": sum(row.get("storageState") == "archived" for row in rows),
        }
        state_filtered = [
            row
            for row in rows
            if (state == "all" or row.get("storageState") == state)
        ]

        projects_by_key: dict[str, dict[str, str]] = {}
        counts: Counter[str] = Counter()
        selectable_counts: Counter[str] = Counter()
        for row in state_filtered:
            info = project_info(row.get("cwd"))
            projects_by_key[info["key"]] = info
            counts[info["key"]] += 1
            if not row.get("ephemeral") and row.get("id") != self.protected_thread_id:
                selectable_counts[info["key"]] += 1
        projects = [
            {
                **projects_by_key[key],
                "count": counts[key],
                "selectableCount": selectable_counts[key],
            }
            for key in sorted(
                projects_by_key,
                key=lambda item: (-counts[item], projects_by_key[item]["name"].casefold()),
            )
        ]

        filtered = [row for row in state_filtered if matches(row, search or None)]

        if project:
            filtered = [
                row for row in filtered if project_info(row.get("cwd"))["key"] == project
            ]

        total = len(filtered)
        pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        items: list[dict[str, Any]] = []
        for index, row in enumerate(filtered[start : start + page_size], start=start + 1):
            item = compact_row(row)
            item["ordinal"] = index
            item["project"] = project_info(row.get("cwd"))
            item["protected"] = bool(
                self.protected_thread_id and row.get("id") == self.protected_thread_id
            )
            item["selectable"] = not item["protected"] and not item["ephemeral"]
            items.append(item)

        return {
            "items": items,
            "page": page,
            "pageSize": page_size,
            "pages": pages,
            "total": total,
            "stats": stats,
            "projects": projects,
            "allSources": all_sources,
        }

    def project_threads(self, project: str, all_sources: bool) -> dict[str, Any]:
        if not project:
            raise ProtocolError("缺少项目标识")
        with self.lock:
            rows = [
                row
                for row in self._load(all_sources)
                if project_info(row.get("cwd"))["key"] == project
            ]
        items: list[dict[str, Any]] = []
        for row in rows:
            item = compact_row(row)
            item["project"] = project_info(row.get("cwd"))
            item["protected"] = bool(
                self.protected_thread_id and row.get("id") == self.protected_thread_id
            )
            item["selectable"] = not item["protected"] and not item["ephemeral"]
            items.append(item)
        return {
            "items": items,
            "total": len(items),
            "selectable": sum(bool(item["selectable"]) for item in items),
        }

    def prepare_delete(
        self, thread_ids: list[Any], confirmed_ids: list[Any]
    ) -> dict[str, Any]:
        """Validate an exact deletion request before Desktop archives its targets."""
        if not thread_ids:
            raise ProtocolError("至少选择一个会话")
        if len(thread_ids) > 10_000:
            raise ProtocolError("单次批量删除不能超过 10000 个会话")
        normalized = [
            validate_uuid(str(value), f"threadIds[{index}]")
            for index, value in enumerate(thread_ids)
        ]
        confirmed = [
            validate_uuid(str(value), f"confirmThreadIds[{index}]")
            for index, value in enumerate(confirmed_ids)
        ]
        if normalized != confirmed:
            raise ProtocolError("确认的会话 UUID 列表与删除目标不一致")
        if len(set(normalized)) != len(normalized):
            raise ProtocolError("批量删除列表包含重复 UUID")
        if self.protected_thread_id and self.protected_thread_id in normalized:
            raise ProtocolError("不能删除当前承载管理界面的会话")

        with self.lock:
            rows = self._load(all_sources=True)
        by_id = {str(row.get("id")): row for row in rows}
        missing = [thread_id for thread_id in normalized if thread_id not in by_id]
        if missing:
            raise ProtocolError(f"有 {len(missing)} 个会话不存在；没有执行删除")
        ephemeral = [
            thread_id for thread_id in normalized if by_id[thread_id].get("ephemeral")
        ]
        if ephemeral:
            raise ProtocolError("批量选择中包含不可删除的临时根会话")
        return {
            "threadIds": normalized,
            "threads": [compact_row(by_id[thread_id]) for thread_id in normalized],
        }

    def delete_many(
        self, thread_ids: list[Any], confirmed_ids: list[Any]
    ) -> dict[str, Any]:
        if not thread_ids:
            raise ProtocolError("至少选择一个会话")
        if len(thread_ids) > 10_000:
            raise ProtocolError("单次批量删除不能超过 10000 个会话")
        normalized = [
            validate_uuid(str(value), f"threadIds[{index}]")
            for index, value in enumerate(thread_ids)
        ]
        confirmed = [
            validate_uuid(str(value), f"confirmThreadIds[{index}]")
            for index, value in enumerate(confirmed_ids)
        ]
        if normalized != confirmed:
            raise ProtocolError("确认的会话 UUID 列表与删除目标不一致")
        if len(set(normalized)) != len(normalized):
            raise ProtocolError("批量删除列表包含重复 UUID")
        if self.protected_thread_id and self.protected_thread_id in normalized:
            raise ProtocolError("不能删除当前承载管理界面的会话")

        with self.lock:
            with AppServer(self.codex_bin, self.codex_home) as server:
                rows = load_threads(server, "all", source_kinds=ALL_SOURCE_KINDS)
                by_id = {str(row.get("id")): row for row in rows}
                missing = [thread_id for thread_id in normalized if thread_id not in by_id]
                if missing:
                    raise ProtocolError(f"有 {len(missing)} 个会话不存在；没有执行删除")
                ephemeral = [
                    thread_id for thread_id in normalized if by_id[thread_id].get("ephemeral")
                ]
                if ephemeral:
                    raise ProtocolError("批量选择中包含不可删除的临时根会话")
                summaries = [compact_row(by_id[thread_id]) for thread_id in normalized]
                selected_ids = set(normalized)

                def selected_depth(thread_id: str) -> int:
                    depth = 0
                    seen: set[str] = set()
                    current = thread_id
                    while current in by_id and current not in seen:
                        seen.add(current)
                        parent = str(by_id[current].get("parentThreadId") or "")
                        if parent not in selected_ids:
                            break
                        depth += 1
                        current = parent
                    return depth

                deletion_order = sorted(normalized, key=selected_depth, reverse=True)
                for thread_id in deletion_order:
                    server.request("thread/delete", {"threadId": thread_id})

            time.sleep(0.2)
            remaining = self._load(all_sources=True)
            remaining_ids = {str(row.get("id")) for row in remaining}
            failed = [thread_id for thread_id in normalized if thread_id in remaining_ids]
            if failed:
                raise ProtocolError(f"删除接口返回成功，但复查时仍存在 {len(failed)} 个会话")

        return {
            "deleted": True,
            "verifiedAbsent": True,
            "count": len(summaries),
            "threads": summaries,
        }

    def delete(self, thread_id: str, confirmed_id: str) -> dict[str, Any]:
        result = self.delete_many([thread_id], [confirmed_id])
        return {
            "deleted": True,
            "verifiedAbsent": True,
            "thread": result["threads"][0],
        }


class UIService:
    def __init__(
        self, store: ThreadStore, token: str, html: str, launcher_html: str
    ) -> None:
        self.store = store
        self.token = token
        self.html = html.encode("utf-8")
        self.launcher_html = launcher_html.encode("utf-8")
        self.origin = ""
        self.last_activity = time.monotonic()
        self.shutdown_requested = False
        self.agent_token = secrets.token_urlsafe(32)
        self._delete_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._pending_deletes: dict[str, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._coordinator_id: str | None = None
        self._coordinator_seen_at = 0.0

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def coordinator_authorized(self, value: str | None) -> bool:
        expected = f"Bearer {self.agent_token}"
        return bool(value and secrets.compare_digest(value, expected))

    def coordinator_ready(self) -> bool:
        with self._pending_lock:
            return (
                self._coordinator_id is not None
                and time.monotonic() - self._coordinator_seen_at
                < COORDINATOR_LEASE_SECONDS
            )

    def mark_coordinator(self, coordinator_id: str) -> None:
        if not coordinator_id:
            raise ProtocolError("缺少侧栏同步协调器标识")
        with self._pending_lock:
            now = time.monotonic()
            lease_active = (
                self._coordinator_id is not None
                and now - self._coordinator_seen_at < COORDINATOR_LEASE_SECONDS
            )
            if lease_active and self._coordinator_id != coordinator_id:
                raise ProtocolError("已有另一个侧栏同步协调器连接")
            self._coordinator_id = coordinator_id
            self._coordinator_seen_at = now
        self.touch()

    def next_delete_request(
        self, coordinator_id: str, timeout: float = 25.0
    ) -> dict[str, Any] | None:
        self.mark_coordinator(coordinator_id)
        if self.shutdown_requested:
            return {"event": "manager_closed"}
        try:
            request = self._delete_queue.get(timeout=timeout)
        except queue.Empty:
            self.mark_coordinator(coordinator_id)
            return None
        self.mark_coordinator(coordinator_id)
        if request is None or self.shutdown_requested:
            return {"event": "manager_closed"}
        return request

    def complete_delete_request(
        self,
        coordinator_id: str,
        request_id: str,
        archived_thread_ids: list[Any],
        error: str | None,
    ) -> None:
        self.mark_coordinator(coordinator_id)
        with self._pending_lock:
            pending = self._pending_deletes.get(request_id)
            if pending is None:
                raise ProtocolError("删除请求不存在或已经超时")
            normalized_archived = [str(value) for value in archived_thread_ids]
            if error is None and normalized_archived != pending["threadIds"]:
                raise ProtocolError("侧栏同步确认的 UUID 列表与删除目标不一致")
            pending["error"] = error
            pending["archivedThreadIds"] = normalized_archived
            pending["event"].set()

    def request_delete(
        self, thread_ids: list[Any], confirmed_ids: list[Any]
    ) -> dict[str, Any]:
        prepared = self.store.prepare_delete(thread_ids, confirmed_ids)
        if not self.coordinator_ready():
            raise ProtocolError(
                "侧栏同步协调器未连接；为避免产生残留条目，本次没有删除"
            )

        request_id = secrets.token_urlsafe(18)
        completed = threading.Event()
        pending = {
            "event": completed,
            "threadIds": prepared["threadIds"],
            "archivedThreadIds": [],
            "error": None,
        }
        with self._pending_lock:
            self._pending_deletes[request_id] = pending
        self._delete_queue.put(
            {
                "event": "archive_required",
                "requestId": request_id,
                "threadIds": prepared["threadIds"],
                "threads": prepared["threads"],
            }
        )

        finished = completed.wait(DELETE_COORDINATION_TIMEOUT_SECONDS)
        with self._pending_lock:
            self._pending_deletes.pop(request_id, None)
        if not finished:
            raise ProtocolError(
                "等待 Codex Desktop 同步侧栏超时；没有执行永久删除"
            )
        if pending["error"]:
            raise ProtocolError(
                f"Codex Desktop 侧栏同步失败；没有执行永久删除：{pending['error']}"
            )

        result = self.store.delete_many(
            prepared["threadIds"], prepared["threadIds"]
        )
        result["desktopSidebarSynced"] = True
        result["desktopCatalogMayRemainStale"] = False
        result["desktopCatalogAutoInvalidationAvailable"] = True
        return result

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        with self._pending_lock:
            pending = list(self._pending_deletes.values())
        for request in pending:
            request["error"] = "管理器已关闭"
            request["event"].set()
        self._delete_queue.put(None)


class Handler(BaseHTTPRequestHandler):
    server_version = "DirectThreadDeleteUI/1"

    @property
    def service(self) -> UIService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, *_: object) -> None:
        return

    def send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, body: bytes, set_session: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        if set_session:
            self.send_header(
                "Set-Cookie",
                f"dtd_session={self.service.token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("dtd_session")
        return bool(morsel and secrets.compare_digest(morsel.value, self.service.token))

    def coordinator_authorized(self) -> bool:
        return self.service.coordinator_authorized(self.headers.get("Authorization"))

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_048_576:
            raise ValueError("invalid request size")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid request payload")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        self.service.touch()
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/agent/next":
            if not self.coordinator_authorized():
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                coordinator_id = self.headers.get("X-Coordinator-Id", "")
                request = self.service.next_delete_request(coordinator_id)
                if request is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                else:
                    self.send_json(HTTPStatus.OK, request)
            except ProtocolError as exc:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return

        if parsed.path == "/health":
            token = query.get("token", [""])[0]
            if not secrets.compare_digest(token, self.service.token):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        supplied_token = query.get("token", [""])[0]
        if parsed.path in {"/", "/launch"} and supplied_token:
            if not secrets.compare_digest(supplied_token, self.service.token):
                self.send_html(HTTPStatus.FORBIDDEN, b"Forbidden")
                return
            if parsed.path == "/launch":
                self.send_html(
                    HTTPStatus.OK,
                    self.service.launcher_html,
                    set_session=True,
                )
                return
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"dtd_session={self.service.token}; HttpOnly; SameSite=Strict; Path=/",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if not self.authorized():
            self.send_html(HTTPStatus.FORBIDDEN, b"Open this page from the plugin launcher.")
            return

        if parsed.path == "/":
            self.send_html(HTTPStatus.OK, self.service.html)
            return

        if parsed.path == "/launch":
            self.send_html(HTTPStatus.OK, self.service.launcher_html)
            return

        if parsed.path == "/api/status":
            self.send_json(
                HTTPStatus.OK,
                {
                    "desktopSyncReady": self.service.coordinator_ready(),
                    "managerVersion": MANAGER_VERSION,
                },
            )
            return

        if parsed.path == "/api/threads":
            try:
                state = query.get("state", ["all"])[0]
                if state not in {"all", "active", "archived"}:
                    raise ValueError("invalid state")
                page = max(1, int(query.get("page", ["1"])[0]))
                page_size = int(query.get("pageSize", ["20"])[0])
                if page_size not in PAGE_SIZES:
                    raise ValueError("invalid page size")
                result = self.service.store.page(
                    page=page,
                    page_size=page_size,
                    state=state,
                    project=query.get("project", [""])[0],
                    search=query.get("search", [""])[0],
                    all_sources=query.get("allSources", ["0"])[0] == "1",
                )
                result["desktopSyncReady"] = self.service.coordinator_ready()
                self.send_json(HTTPStatus.OK, result)
            except (ValueError, ProtocolError, OSError, subprocess.SubprocessError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if parsed.path == "/api/project-threads":
            try:
                result = self.service.store.project_threads(
                    project=query.get("project", [""])[0],
                    all_sources=query.get("allSources", ["0"])[0] == "1",
                )
                self.send_json(HTTPStatus.OK, result)
            except (ValueError, ProtocolError, OSError, subprocess.SubprocessError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        self.service.touch()
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/agent/complete":
            if not self.coordinator_authorized():
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                payload = self.read_json_body()
                archived_ids = payload.get("archivedThreadIds", [])
                if not isinstance(archived_ids, list):
                    raise ValueError("archivedThreadIds must be an array")
                error = payload.get("error")
                if error is not None and not isinstance(error, str):
                    raise ValueError("error must be a string or null")
                self.service.complete_delete_request(
                    self.headers.get("X-Coordinator-Id", ""),
                    str(payload.get("requestId", "")),
                    archived_ids,
                    error,
                )
                self.send_json(HTTPStatus.OK, {"accepted": True})
            except (ValueError, json.JSONDecodeError, ProtocolError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if not self.authorized():
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        origin = self.headers.get("Origin")
        if origin and origin != self.service.origin:
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "origin rejected"})
            return
        if parsed.path == "/api/shutdown":
            self.service.request_shutdown()
            self.send_json(HTTPStatus.OK, {"closed": True})
            return
        if parsed.path not in {"/api/delete", "/api/delete-many"}:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            payload = self.read_json_body()
            if parsed.path == "/api/delete-many":
                thread_ids = payload.get("threadIds")
                confirmed_ids = payload.get("confirmThreadIds")
                if not isinstance(thread_ids, list) or not isinstance(confirmed_ids, list):
                    raise ValueError("threadIds and confirmThreadIds must be arrays")
                result = self.service.request_delete(thread_ids, confirmed_ids)
            else:
                result = self.service.request_delete(
                    [str(payload.get("threadId", ""))],
                    [str(payload.get("confirmThreadId", ""))],
                )
                result["thread"] = result["threads"][0]
            self.send_json(HTTPStatus.OK, result)
        except (ValueError, json.JSONDecodeError, ProtocolError, OSError, subprocess.SubprocessError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def state_path_default(protected_thread_id: str | None, codex_home: str) -> Path:
    identity = (
        f"v{MANAGER_VERSION}\0{codex_home.casefold()}\0{protected_thread_id or ''}"
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "direct-thread-delete-ui" / f"state-{suffix}.json"


def read_state(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def state_is_live(state: dict[str, Any]) -> bool:
    health_url = state.get("healthUrl")
    if not isinstance(health_url, str):
        return False
    try:
        with urllib.request.urlopen(health_url, timeout=1.5) as response:
            return response.status == HTTPStatus.OK
    except (OSError, urllib.error.URLError):
        return False


def state_matches(
    state: dict[str, Any],
    codex_home: str,
    codex_bin: str,
    protected_thread_id: str | None,
) -> bool:
    return (
        state.get("managerVersion") == MANAGER_VERSION
        and state.get("ready") is True
        and state.get("codexHome") == codex_home
        and state.get("codexBin") == codex_bin
        and state.get("protectedThreadId") == protected_thread_id
    )


def read_log_tail(path: Path, max_chars: int = 4000) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return content[-max_chars:]


def coordinator_request(
    url: str,
    token: str,
    coordinator_id: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 35.0,
) -> tuple[int, dict[str, Any] | None]:
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Coordinator-Id": coordinator_id,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == HTTPStatus.NO_CONTENT:
                return response.status, None
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            details = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            details = body
        raise ProtocolError(f"侧栏同步协调器请求失败 ({exc.code}): {details}") from exc


def run_coordinate(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).expanduser().resolve()
    state = read_state(state_file)
    if not state or not state_is_live(state):
        raise ProtocolError("会话管理器未运行或状态文件已失效")
    origin = state.get("agentOrigin")
    token = state.get("agentToken")
    if not isinstance(origin, str) or not isinstance(token, str):
        raise ProtocolError("当前管理器版本不支持侧栏同步协调")

    coordinator_id = secrets.token_urlsafe(18)
    print(
        json.dumps(
            {
                "event": "coordinator_ready",
                "managerVersion": state.get("managerVersion"),
                "stateFile": str(state_file),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    while True:
        try:
            _, event = coordinator_request(
                f"{origin}/api/agent/next",
                token,
                coordinator_id,
                timeout=35.0,
            )
        except (OSError, urllib.error.URLError) as exc:
            if not state_is_live(state):
                print(json.dumps({"event": "manager_closed"}), flush=True)
                return 0
            raise ProtocolError(f"无法连接会话管理器：{exc}") from exc
        if event is None:
            continue
        print(json.dumps(event, ensure_ascii=False), flush=True)
        if event.get("event") == "manager_closed":
            return 0
        if event.get("event") != "archive_required":
            continue

        line = sys.stdin.readline()
        if not line:
            raise ProtocolError("侧栏同步协调器输入已关闭")
        try:
            completion = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError("侧栏同步协调器收到无效 JSON") from exc
        if not isinstance(completion, dict):
            raise ProtocolError("侧栏同步协调器完成消息必须是对象")
        if completion.get("requestId") != event.get("requestId"):
            raise ProtocolError("侧栏同步协调器完成消息的 requestId 不匹配")
        coordinator_request(
            f"{origin}/api/agent/complete",
            token,
            coordinator_id,
            payload=completion,
            timeout=10.0,
        )
        print(
            json.dumps(
                {
                    "event": "completion_received",
                    "requestId": event.get("requestId"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def run_launch(args: argparse.Namespace) -> int:
    codex_home = str(resolve_codex_home(args.codex_home))
    protected_thread_id = (
        validate_uuid(args.protect_thread_id, "--protect-thread-id")
        if args.protect_thread_id
        else None
    )
    state_file = Path(
        args.state_file or state_path_default(protected_thread_id, codex_home)
    ).expanduser().resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    existing = read_state(state_file)
    if existing and state_is_live(existing) and state_matches(
        existing, codex_home, args.codex_bin, protected_thread_id
    ):
        existing["reused"] = True
        print(json.dumps(existing, ensure_ascii=False, indent=2))
        return 0
    if existing and state_is_live(existing):
        raise ProtocolError(
            "A different manager is already using the requested state file; "
            "it was left running"
        )

    try:
        state_file.unlink(missing_ok=True)
    except OSError:
        pass

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "serve",
        "--state-file",
        str(state_file),
        "--idle-timeout",
        str(args.idle_timeout),
        "--codex-bin",
        args.codex_bin,
    ]
    command.extend(["--codex-home", codex_home])
    if protected_thread_id:
        command.extend(["--protect-thread-id", protected_thread_id])

    log_file = state_file.with_suffix(".log")
    log_handle = open(log_file, "w", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    log_handle.close()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = read_state(state_file)
        if state and state.get("pid") == process.pid and state_is_live(state):
            state["reused"] = False
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.1)
    details = read_log_tail(log_file)
    detail_suffix = f": {details}" if details else f"; inspect {log_file}"
    raise ProtocolError(f"UI readiness check failed{detail_suffix}")


def run_serve(args: argparse.Namespace) -> int:
    asset_dir = Path(__file__).resolve().parent.parent / "assets"
    html = (asset_dir / "index.html").read_text(encoding="utf-8")
    launcher_html = (asset_dir / "launcher.html").read_text(encoding="utf-8")
    token = secrets.token_urlsafe(32)
    protected = (
        validate_uuid(args.protect_thread_id, "--protect-thread-id")
        if args.protect_thread_id
        else None
    )
    store = ThreadStore(args.codex_bin, args.codex_home, protected)
    initial_thread_count = store.preflight()
    service = UIService(store, token, html, launcher_html)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.service = service  # type: ignore[attr-defined]
    server.timeout = 1
    port = int(server.server_address[1])
    service.origin = f"http://127.0.0.1:{port}"

    state_file = Path(args.state_file).expanduser().resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "managerVersion": MANAGER_VERSION,
        "ready": True,
        "initialThreadCount": initial_thread_count,
        "pid": os.getpid(),
        "url": f"{service.origin}/launch?token={urllib.parse.quote(token)}",
        "managerUrl": f"{service.origin}/?token={urllib.parse.quote(token)}",
        "healthUrl": f"{service.origin}/health?token={urllib.parse.quote(token)}",
        "origin": service.origin,
        "agentOrigin": service.origin,
        "agentToken": service.agent_token,
        "stateFile": str(state_file),
        "startedAt": int(time.time()),
        "codexHome": str(resolve_codex_home(args.codex_home)),
        "codexBin": args.codex_bin,
        "protectedThreadId": protected,
    }
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, state_file)

    try:
        while (
            not service.shutdown_requested
            and time.monotonic() - service.last_activity < args.idle_timeout
        ):
            server.handle_request()
    finally:
        server.server_close()
        current = read_state(state_file)
        if current and current.get("pid") == os.getpid():
            state_file.unlink(missing_ok=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the local Codex task deletion UI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch = subparsers.add_parser("launch", help="Start or reuse a background UI server")
    launch.add_argument("--codex-bin", default="codex")
    launch.add_argument("--codex-home")
    launch.add_argument("--protect-thread-id")
    launch.add_argument("--idle-timeout", type=int, default=1800)
    launch.add_argument("--state-file")
    launch.set_defaults(handler=run_launch)

    serve = subparsers.add_parser("serve", help="Run the loopback HTTP server")
    serve.add_argument("--codex-bin", default="codex")
    serve.add_argument("--codex-home")
    serve.add_argument("--protect-thread-id")
    serve.add_argument("--idle-timeout", type=int, default=1800)
    serve.add_argument("--state-file", required=True)
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(handler=run_serve)

    coordinate = subparsers.add_parser(
        "coordinate", help="Coordinate Desktop sidebar removal with UI deletions"
    )
    coordinate.add_argument("--state-file", required=True)
    coordinate.set_defaults(handler=run_coordinate)
    return parser


def main() -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (ProtocolError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
