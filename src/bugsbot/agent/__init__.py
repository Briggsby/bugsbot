"""Claude Agent SDK workers for bugsbot.

- `policy`: the hard rules — system prompt and the shell-command guard.
- `session`: SDK options and message logging shared by every worker.
- `tools`: the signed GitHub/git tools workers use for all writes.
- `prompts`: task prompts for issues and pull requests.
- `run`: one autonomous run for one issue or PR.
"""
