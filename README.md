# Direct Thread Delete

[中文文档](docs/README.zh-CN.md)

A local Codex plugin for browsing tasks by project and permanently deleting selected tasks while keeping the Codex Desktop sidebar synchronized.

## Install

```powershell
codex plugin marketplace add Lucency09/direct-thread-delete
codex plugin add direct-thread-delete@lucency09
```

Restart Codex Desktop after installation, start a new task, and invoke `@Direct Thread Delete`.

## Safety

- Deletion is permanent and requires exact-target confirmation in the manager UI.
- The task hosting the manager is protected from deletion.
- The manager binds to loopback only.
- The Python implementation uses only the standard library and does not read or modify `auth.json`.

## Repository layout

```text
.agents/plugins/marketplace.json
plugins/direct-thread-delete/
```
