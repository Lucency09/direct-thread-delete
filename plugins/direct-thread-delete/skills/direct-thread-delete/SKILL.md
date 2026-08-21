---
name: direct-thread-delete
description: Open a project-tree local UI to browse, filter, paginate, select, synchronize the Desktop sidebar, and permanently delete saved Codex tasks, including active or archived threads. Also supports direct UUID deletion. Use when the user asks to manage, remove, or delete Codex tasks, conversations, sessions, or threads. Do not use for ChatGPT web chats or ordinary project files.
---

# Direct Thread Delete

Resolve bundled paths relative to this `SKILL.md` file.

This skill targets the Codex home used by the `codex` executable. It calls the supported local app-server protocol and does not inspect or modify `auth.json`; ChatGPT-account and API-key login modes follow the same deletion path. Every `thread/list` request must pass an empty `modelProviders` array, which means all model providers; omitting it can hide tasks created under an older account or API provider.

## UI workflow

Use the UI by default when the user asks for a list, management screen, project grouping, or multiple deletions.

If the user's message contains only the plugin mention, treat that as an explicit request to launch and open the UI immediately. Do not ask which thread they want to delete, and do not stop after printing or describing the URL.

1. Launch the local-only manager:

   ```powershell
   python <skill-dir>\scripts\session_ui.py launch --protect-thread-id <current-task-uuid>
   ```

   **Required Windows Codex Desktop permission:** run this launch command outside the task filesystem sandbox on the first attempt, with permission to initialize and use the current Windows user's native `~\.codex` state directory and the per-user temporary directory. Request the narrowly scoped command/filesystem approval before launching when it has not already been remembered. Do not make an initial sandboxed launch attempt. The detached backend and its `codex app-server` children inherit this permission, so refresh, listing, and deletion must use the same native Codex state without another sandbox fallback.

   This permission authorizes Codex state access required by the supported `thread/list` and `thread/delete` app-server methods; it does not authorize reading, copying, displaying, or modifying `auth.json` or other credentials.

   Omit `--protect-thread-id` only when the current task UUID is genuinely unavailable. By default the launcher uses the current operating-system user's native `~/.codex` instead of inheriting a task-isolation `CODEX_HOME`; append `--codex-home <path>` only when the user explicitly selected another installation. On Windows, use `py -3` if `python` is unavailable; if neither command is registered, load the Codex Desktop workspace dependencies and use its bundled Python executable. The implementation uses only Python's standard library.

   The launcher completes a real `thread/list` readiness check before it returns a URL. Do not open a browser tab unless the command exits successfully and its JSON contains `ready: true`. If readiness still fails despite the required Windows permission, stop and report the error; do not expose a failed page, change `CODEX_HOME`, or click a manager shutdown control as a recovery mechanism.

   The successful command returns JSON whose `url` is a loopback bootstrap page. In Codex's in-app browser, open that URL, immediately activate its **打开会话管理器** link, switch to or claim the newly opened manager tab, close only the bootstrap tab, and leave the manager tab visible for the user. Perform these browser steps yourself; do not ask the user to click through the bootstrap. Opening the manager through this link is required so its **关闭管理器** button can close both the backend and its own tab.

2. Start the foreground sidebar coordinator using the exact `stateFile` returned by the launcher:

   ```powershell
   python <skill-dir>\scripts\session_ui.py coordinate --state-file "<stateFile>"
   ```

   Run it through a long-lived command session with a PTY/interactive stdin (`tty: true`), not as a detached process. Wait for its `coordinator_ready` JSON event before handing over the manager page. The coordinator uses only a loopback bearer token stored in the manager's version-scoped temporary state file.

3. The UI lists all user-visible CLI and Codex app tasks, groups them in collapsible project trees, and provides search, state/project filters, page-size controls, pagination, refresh, UUID copy, row selection, project-wide selection across pages, batch deletion, and a delete button on every row. Deletion remains disabled unless the sidebar coordinator is connected.

4. The **关闭管理器** button stops the local Python backend, ends the coordinator, and closes its manager tab. Closing only the browser tab keeps the backend and coordinator available for reuse; the backend exits automatically after 30 minutes without requests.

5. Each row delete button must open the UI's exact-target confirmation dialog. Batch deletion must show the selected count and preview, and the backend rejects a request unless the ordered `threadIds` and `confirmThreadIds` lists match exactly. This dialog is the user's destructive-action confirmation; do not ask for a second confirmation in chat.

6. Keep the coordinator session alive while the manager is open. Poll it with the command-session input tool using waits no longer than 50 seconds. When it emits an `archive_required` event:

   - Copy the ordered `threadIds` exactly as emitted.
   - For every item whose `storageState` is `active`, call Codex's `set_thread_archived` task tool with `archived: true` and `hostId: "local"`. Items already marked `archived` need no host action. This host-side action is required because only Desktop can update its live sidebar catalog.
   - Only after every required archive call succeeds, write one JSON line to the coordinator's stdin containing the same `requestId`, the same ordered IDs as `archivedThreadIds`, and `"error": null`.
   - If any archive call fails, best-effort unarchive the originally active IDs already archived during this request, then write one JSON line with the matching `requestId`, an empty `archivedThreadIds` array, and a concise non-empty `error`; the backend will not permanently delete anything.
   - Wait for `completion_received`, then continue monitoring for the next request.

   Example success acknowledgement:

   ```json
   {"requestId":"<request-id>","archivedThreadIds":["<uuid>"],"error":null}
   ```

   The backend then calls supported `thread/delete`, verifies every target is absent, and reports success to the UI. This is intentionally one user action even though Desktop performs a host-side archive transition internally to keep its sidebar synchronized.

7. Keep the UI task open while the user manages other tasks. Warn the user not to delete the task that launched the UI; use a different task to host the manager.

8. After the manager tab has loaded successfully, do not click **关闭管理器**, close its tab, stop its backend, or run browser cleanup against it unless the user explicitly asks to close it. Continue servicing coordinator events until the user closes the manager or its backend exits. Send the final response only after the coordinator reports `manager_closed` or its process exits.

The launcher also never shuts down a live manager to recover from a version or state mismatch. Default state files are version-scoped; an older manager may remain open until the user closes it or its idle timeout expires.

On Windows, the detached manager and every `codex app-server` child are started with `CREATE_NO_WINDOW`; preserve this behavior so refresh and deletion never open a console window. Always apply the required native-Codex-home permission before the first launch instead of discovering the sandbox denial through a failed UI.

## Command-line workflow

Use `scripts/thread_admin.py` when the user requests a scriptable or UUID-only operation.

1. Locate candidates with the read-only command:

   ```powershell
   python <skill-dir>\scripts\thread_admin.py list --state all --search "<title-or-id>"
   ```

   On Windows, use `py -3` if `python` is unavailable. Use `--json` when structured output is more useful. The default list contains all user-visible CLI and Codex app tasks with no result cap; add `--max-results` only when the user requests a limit. Add `--all-sources` only when the user explicitly wants internal subagent or review threads.

2. Identify exactly one persisted UUID. Show the user its title or preview, UUID, and whether it is active or archived. Do not guess when several candidates match.

3. Immediately before deletion, ask the user to confirm that exact UUID. Explain that the operation permanently removes the thread and its spawned descendants. Earlier general permission does not replace this target-specific confirmation.

4. After confirmation, pass the same UUID twice:

   ```powershell
   python <skill-dir>\scripts\thread_admin.py delete --thread-id <uuid> --confirm <uuid>
   ```

5. Report whether deletion and post-delete verification succeeded.

## Constraints

- In the UI workflow, use Codex's host-side archive action only as the synchronization phase of the same confirmed delete operation. Never require the user to archive manually.
- In the command-line UUID workflow, continue to delete directly without an archive-first step; warn that Desktop sidebar synchronization is only guaranteed by the coordinated UI workflow.
- Never manually remove session JSONL files or edit the Codex state database.
- Never read, copy, or change authentication credentials.
- Keep the bundled Python implementation standard-library-only. Do not install or require third-party Python packages.
- Do not delete the task currently executing this skill. Ask the user to run the deletion from a different task so the result can be verified safely.
- Bind the UI to loopback only. Do not expose it to the LAN or Internet.
- Use the native per-user `~/.codex` for the operating system running Python. Use `--codex-home <path>` only when the user explicitly selects a different Codex installation.
- If the installed CLI lacks the required app-server methods, stop and report the minimum required upgrade instead of falling back to raw filesystem deletion.
