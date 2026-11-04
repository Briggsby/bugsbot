"""Cursor.

- `provider`: CursorProvider, what bugsbot uses (see bugsbot.providers.base).
- `agent`: CursorAgent, issue/PR runs through Cursor's headless CLI on this host.
- `hook`: CursorHook, the shell-command check in Cursor's beforeShellExecution hook format.
- `guardrails`: bugsbot's tools and command check as project-local Cursor config.
- `cloud`: remote workers, as Cursor Cloud Agents running in Cursor's cloud.
- `api`: a client for Cursor's Cloud Agents API.
- `credentials`: Cursor API key checks, the CLI's location, and login hints.
"""
