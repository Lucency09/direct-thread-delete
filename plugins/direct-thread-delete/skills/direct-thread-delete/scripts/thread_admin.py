#!/usr/bin/env python3
"""List or directly delete persisted Codex threads through codex app-server."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


INTERACTIVE_SOURCE_KINDS = ["cli", "vscode"]

ALL_SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]


class ProtocolError(RuntimeError):
    pass


def resolve_codex_home(value: str | None = None) -> Path:
    """Select the native per-user Codex home unless explicitly overridden."""
    candidate = Path(value).expanduser() if value else Path.home() / ".codex"
    return candidate.resolve()


class AppServer:
    def __init__(self, codex_bin: str, codex_home: str | None = None) -> None:
        resolved = shutil.which(codex_bin)
        if not resolved:
            raise ProtocolError(f"Codex CLI not found: {codex_bin}")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(resolve_codex_home(codex_home))

        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
        self._proc = subprocess.Popen(
            [resolved, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **process_options,
        )
        self._next_id = 1

        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "direct_thread_delete",
                    "title": "Direct Thread Delete",
                    "version": "0.1.0",
                }
            },
        )
        self.notify("initialized", {})

    def _write(self, message: dict[str, Any]) -> None:
        if self._proc.stdin is None:
            raise ProtocolError("Codex app-server stdin is unavailable")
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._write(message)

        if self._proc.stdout is None:
            raise ProtocolError("Codex app-server stdout is unavailable")

        while True:
            line = self._proc.stdout.readline()
            if not line:
                self._stderr.seek(0)
                details = self._stderr.read().strip()
                suffix = f": {details}" if details else ""
                raise ProtocolError(f"Codex app-server exited before replying{suffix}")
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                raise ProtocolError(
                    f"{method} failed: {error.get('message', error)}"
                    if isinstance(error, dict)
                    else f"{method} failed: {error}"
                )
            return response.get("result")

    def close(self) -> None:
        if self._proc.stdin is not None:
            self._proc.stdin.close()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
        self._stderr.close()

    def __enter__(self) -> "AppServer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def list_state(
    server: AppServer, archived: bool, source_kinds: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {
            "archived": archived,
            "limit": 100,
            "modelProviders": [],
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "sourceKinds": source_kinds,
        }
        if cursor:
            params["cursor"] = cursor
        result = server.request("thread/list", params) or {}
        for row in result.get("data", []):
            item = dict(row)
            item["storageState"] = "archived" if archived else "active"
            rows.append(item)
        cursor = result.get("nextCursor")
        if not cursor:
            return rows
        if cursor in seen_cursors:
            raise ProtocolError("thread/list returned a repeated pagination cursor")
        seen_cursors.add(cursor)


def load_threads(
    server: AppServer,
    state: str,
    source_kinds: list[str] | None = None,
) -> list[dict[str, Any]]:
    selected_sources = source_kinds or ALL_SOURCE_KINDS
    rows: list[dict[str, Any]] = []
    if state in {"active", "all"}:
        rows.extend(list_state(server, archived=False, source_kinds=selected_sources))
    if state in {"archived", "all"}:
        rows.extend(list_state(server, archived=True, source_kinds=selected_sources))
    return rows


def title_for(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("preview") or "(untitled)").replace("\t", " ").replace("\n", " ")


def matches(row: dict[str, Any], query: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold()
    haystack = "\n".join(
        str(row.get(key) or "") for key in ("id", "name", "preview", "cwd")
    ).casefold()
    return needle in haystack


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    title = title_for(row)
    if len(title) > 160:
        title = title[:157] + "..."
    return {
        "id": row.get("id"),
        "title": title,
        "storageState": row.get("storageState"),
        "runtimeStatus": (row.get("status") or {}).get("type") if isinstance(row.get("status"), dict) else row.get("status"),
        "createdAt": row.get("createdAt"),
        "updatedAt": row.get("updatedAt"),
        "cwd": row.get("cwd"),
        "source": row.get("source"),
        "ephemeral": bool(row.get("ephemeral", False)),
    }


def print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No matching persisted Codex threads.")
        return
    print("STATE\tTITLE\tTHREAD_ID")
    for row in rows:
        title = title_for(row)
        if len(title) > 90:
            title = title[:87] + "..."
        print(f"{row.get('storageState', '')}\t{title}\t{row.get('id', '')}")


def validate_uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ProtocolError(f"{label} must be a full Codex thread UUID") from exc


def run_list(args: argparse.Namespace) -> int:
    with AppServer(args.codex_bin, args.codex_home) as server:
        source_kinds = ALL_SOURCE_KINDS if args.all_sources else INTERACTIVE_SOURCE_KINDS
        rows = [
            row
            for row in load_threads(server, args.state, source_kinds=source_kinds)
            if matches(row, args.search)
        ]
    rows.sort(key=lambda row: row.get("updatedAt") or row.get("createdAt") or 0, reverse=True)
    if args.max_results is not None:
        rows = rows[: args.max_results]
    if args.json:
        print(json.dumps([compact_row(row) for row in rows], ensure_ascii=False, indent=2))
    else:
        print_table(rows)
    return 0


def run_delete(args: argparse.Namespace) -> int:
    thread_id = validate_uuid(args.thread_id, "--thread-id")
    confirmed_id = validate_uuid(args.confirm, "--confirm")
    if thread_id != confirmed_id:
        raise ProtocolError("--confirm must exactly match --thread-id")

    with AppServer(args.codex_bin, args.codex_home) as server:
        rows = load_threads(server, "all")
        target = next((row for row in rows if str(row.get("id")) == thread_id), None)
        if target is None:
            raise ProtocolError("Thread was not found in active or archived storage; nothing was deleted")
        if target.get("ephemeral"):
            raise ProtocolError("Ephemeral root threads cannot be deleted")

        summary = compact_row(target)
        server.request("thread/delete", {"threadId": thread_id})

    time.sleep(0.2)
    with AppServer(args.codex_bin, args.codex_home) as verifier:
        still_present = any(
            str(row.get("id")) == thread_id for row in load_threads(verifier, "all")
        )
    if still_present:
        raise ProtocolError("Delete returned success, but the thread is still listed")

    print(json.dumps({"deleted": True, "verifiedAbsent": True, "thread": summary}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List or permanently delete persisted Codex threads without archiving first."
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable name or path")
    parser.add_argument("--codex-home", help="Explicit CODEX_HOME override")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List persisted threads (read-only)")
    list_parser.add_argument("--state", choices=("active", "archived", "all"), default="all")
    list_parser.add_argument("--search", help="Case-insensitive local match on id, title, preview, or cwd")
    list_parser.add_argument("--max-results", type=int, help="Return only the newest N matches")
    list_parser.add_argument(
        "--all-sources",
        action="store_true",
        help="Include subagents, review agents, exec sessions, and other internal sources",
    )
    list_parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    list_parser.set_defaults(handler=run_list)

    delete_parser = subparsers.add_parser("delete", help="Permanently delete one thread UUID")
    delete_parser.add_argument("--thread-id", required=True)
    delete_parser.add_argument("--confirm", required=True, help="Must exactly repeat --thread-id")
    delete_parser.set_defaults(handler=run_delete)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (ProtocolError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
