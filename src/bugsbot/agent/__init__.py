"""Provider-neutral agent pieces for bugsbot. The code that drives a particular agent (Claude
Code, Cursor) lives in bugsbot.providers.

- `policy`: the hard rules, and the shell-command check every provider's hook applies.
- `tools`: the signed GitHub/git tools agents use for all writes.
- `toolspec`: which tools a run gets, serialisable for tool servers in another process.
- `mcp_stdio`: those tools as a stdio MCP server, for agents bugsbot doesn't run in-process.
- `hook`: the command check as a hook command, in each provider's input/output format.
- `prompts`: task prompts for issues and pull requests.
- `run`: one autonomous run for one issue or PR, on the configured provider.
"""
