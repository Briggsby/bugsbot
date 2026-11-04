"""Claude Code.

- `provider`: ClaudeProvider, what bugsbot uses (see bugsbot.providers.base).
- `agent`: ClaudeAgent, issue/PR runs through the Claude Agent SDK, with bugsbot's tools in-process.
- `hook`: ClaudeHook, the shell-command check in Claude Code's PreToolUse hook format.
- `remote_control`: remote workers, as Claude Code Remote Control sessions on this host.
- `guardrails`: bugsbot's rules as project-local Claude Code config, for Remote Control workers.
- `trust`: Claude Code's per-folder trust records, for worker checkouts.
- `credentials`: Anthropic API key checks and login hints.
"""
