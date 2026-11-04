# bugsbot
AI Agent Harness for Github PR creation and editing

bugsbot watches GitHub repositories for issues and pull requests that carry labels you choose,
and spins up [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk) workers to handle them:

- **Labelled issues**: the agent comments with a plan, implements the fix on a
  `bugsbot/issue-N` branch, and opens PR(s). The PRs get the same label as the issue. It also
  answers follow-up comments on the issue.
- **Labelled PRs**, including the ones bugsbot opened: the agent collects every comment it hasn't
  replied to directly (conversation comments, review summaries and unresolved inline review
  threads) and reads them all. It replies to each one with an assessment of whether it's correct
  and a change is justified, makes and pushes the commits, then replies again saying what it did.
- **Remote workers**: start a [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control)
  session on the bugsbot host from the web UI, then chat with it from the Claude app
  (claude.ai/code, desktop or mobile). See [Remote workers](#remote-workers).
- **No merging, ever.** The agents can't merge PRs (see [Safety](#safety)).

Every comment, reply and PR description bugsbot posts ends with a configurable signature and a
hidden `<!-- bugsbot -->` marker. bugsbot uses the marker to recognise its own comments.

## Running

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and git.

```sh
uv run bugsbot            # opens the web UI in your browser, already signed in
```

The only interactive sign-in is **GitHub OAuth**. To get in before that's set up, the server prints
a single-use sign-in link (valid for 10 minutes) and opens your browser with it. Run
`uv run bugsbot login-link` (with the same `--home`, and `--port`/`--public-url` as the server) to
mint another one at any time, e.g. if OAuth is misconfigured.

In the UI:

1. **Settings → GitHub sign-in**: register an OAuth App and choose who's allowed in (see below).
2. **Settings**: add your Anthropic API key (or a Cursor API key, to use Cursor; see
   [Agent providers](#agent-providers)). If you leave it blank, Claude Code's own login is used.
   You can also add a GitHub token; if blank, your `gh auth login` session is used. The token needs
   repo contents, issues and pull requests read/write access.
3. **Repos & labels**: pick repositories (use "Load my repos" to choose from yours) and add one or
   more labels, e.g. `bugsbot`.
4. Label an issue or PR on GitHub. It shows up under **Issues** or **Pull requests** on the next
   poll, and each agent it starts is listed under **Agent runs** with its duration, turns, cost and
   full log.

State (settings, repos, labels, tracked items, run history) lives in `~/.bugsbot/bugsbot.db`
(`--home` or `$BUGSBOT_HOME` to change), so it survives restarts. Agent checkouts live in
`~/.bugsbot/workspaces/`.

To run it on a server instead, the **Deploy** GitHub Actions workflow sets up a Hetzner Cloud
server end to end, and `deploy/local.sh` sets up the machine you run it on; see
[deploy/README.md](deploy/README.md).

### Options

| Flag | Default | |
|---|---|---|
| `--host` | `127.0.0.1` | Interface to bind; `0.0.0.0` to expose it |
| `--port` | `8765` | bugsbot exits with an error if the port is already in use |
| `--public-url` | | External URL when behind a reverse proxy (used in the printed sign-in link) |
| `--forwarded-allow-ips` | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` headers |
| `--no-browser` | | Don't open the UI automatically |

### Exposing it publicly

Every page and API endpoint requires authentication:

- **Sign in with GitHub**. Create an
  [OAuth App](https://github.com/settings/developers) whose callback URL is
  `<public-url>/auth/github/callback`; **Settings → GitHub sign-in** shows the exact URL. Enter the
  client ID and secret there, then allow specific logins, members of organisations, and/or anyone
  with write access to a tracked repo. Nobody is allowed by default. GitHub sessions last 24 hours.
- Single-use sign-in links, which only someone with shell access to the server can mint:
  `bugsbot login-link`. They're stored hashed and expire after 10 minutes.
- Signed `HttpOnly`, `SameSite=Strict` session cookies, marked `Secure` over HTTPS.
- Optionally an API token for scripts: `Authorization: Bearer <token>`. Generate it under
  **Settings → Access**; it's stored hashed.
- Every write must carry an `X-Bugsbot: 1` header (CSRF protection).

Put it behind a TLS-terminating reverse proxy, e.g.
`bugsbot --host 127.0.0.1 --public-url https://bugsbot.example.com --no-browser` with your proxy
forwarding to it. Don't serve it over plain HTTP on the internet.

## Agent providers

bugsbot can run its agents on **Claude Code** or **Cursor**. Choose under **Settings → Agent
behaviour → Agent for issues & PRs**; each run records which provider it used.

| | Claude Code | Cursor |
|---|---|---|
| Issue and PR runs | Claude Agent SDK, on this host | Cursor's headless CLI (`agent -p`), on this host |
| Credentials | Anthropic API key, or the host's Claude Code login | Cursor API key, or the host's `agent login` |
| bugsbot's tools, hook and credential isolation | Yes | Yes |
| Limits | Max turns, USD budget, time | Time only (the CLI reports no turns or cost) |
| Remote workers | Claude Code Remote Control, on this host | Cursor Cloud Agents, in Cursor's cloud |

For Cursor runs, install the CLI on the host (`curl https://cursor.com/install -fsS | bash`) and
add a Cursor API key (Cursor dashboard → Integrations → User API Keys) under **Settings →
Credentials**. bugsbot gives the CLI its tools as an MCP server in `.cursor/mcp.json`, and its
command check as a `beforeShellExecution` hook in `.cursor/hooks.json`. It writes both into the
checkout and hides them from git. The rules go at the top of the prompt.

The code for each provider lives in its own package under `src/bugsbot/providers/`, behind a common
interface (`providers/base.py`).

## Remote workers

A remote worker is an agent you chat with in its provider's own app, not in bugsbot. bugsbot starts
it, links to it, tracks how long it runs, and can stop it. Pick the kind on the **Remote workers**
tab.

### Claude Code Remote Control

This starts `claude remote-control` on the bugsbot host, in a fresh
checkout of a tracked repo (or an empty scratch folder). You talk to the worker from the Claude app,
not from bugsbot. bugsbot shows an **Open in Claude** link once the session connects, records its
start-up output and duration, and can stop it.

- The host must be logged in to Claude Code (`claude` then `/login`) with a Claude subscription
  account. The worker appears in **that account's** Claude app, and only that account can open it.
  Usage counts against that subscription, so bugsbot doesn't know the worker's cost.
- **Branch** is optional. An existing branch is checked out; a new name is created from the default
  branch. If it's blank, the worker starts on the default branch (which it can't push to) and
  creates a branch named for the task once you've told it what to do.
- **"Enable Remote Control?"** is answered for you. Before starting a worker, bugsbot sets
  Claude Code's `remoteDialogSeen` flag in `~/.claude.json`, which is internal to Claude Code and
  not a public setting. If the question appears anyway, bugsbot answers yes and logs which key it
  pressed. This is the only prompt bugsbot answers.
- **Folder trust:** Claude Code asks whether to trust each new folder, and trust isn't inherited
  from parent folders. Each worker gets a fresh checkout under `~/.bugsbot/workers/`, so just before
  starting a worker bugsbot records trust for that one folder in Claude Code's config
  (`~/.claude.json`, the same flag accepting the prompt sets). It removes the entry when the worker
  ends. It never touches entries outside the workers folder, and writes atomically. If another
  Claude Code process overwrites the change at the same moment and the prompt appears anyway, the
  worker fails immediately with a message to start it again. bugsbot never answers the prompt
  itself.
- The worker gets bugsbot's rules through project-local Claude Code config in the checkout,
  hidden from git: the no-merge/no-push hook, the signed `push_branch`/`open_pull_request`/
  `comment` tools, and the hard rules in `CLAUDE.local.md`.
- The session link is read from Claude Code's local session records, which aren't a public
  format. If it's missing, look for the worker by name in the Claude app's Code section.

### Cursor Cloud Agents

This launches a [Cursor Cloud Agent](https://cursor.com/docs/cloud-agent) through Cursor's API.
It runs on Cursor's machines against a tracked repo, and you chat with it at
[cursor.com/agents](https://cursor.com/agents) in any browser, or in Cursor. It needs a Cursor API
key, and usage counts against that key's Cursor account.

- **First message:** you give it one, which tells it what to start on. After that you talk to it in
  Cursor. bugsbot doesn't record the conversation, only the agent's status and the branches and PRs
  it reports.
- **Branch:** name an existing branch to work on it, or a new name, which bugsbot creates from the
  default branch on GitHub first. Leave it blank and Cursor makes a `cursor/…` branch. Protected
  branches are refused.
- **Stopping:** stopping it from bugsbot cancels its current run and archives it in Cursor. You can
  unarchive it there. Archiving or deleting it in Cursor ends it in bugsbot too.
- **Restarts:** it keeps running if bugsbot restarts, and bugsbot resumes tracking it.
- **bugsbot's guardrails don't apply.** The agent runs outside this host and pushes with Cursor's
  GitHub app, so bugsbot can't block merges, isolate credentials or sign its comments. It's only
  instructed not to merge or push to the default branch, and to add the signature. Protect your
  default branch with a GitHub ruleset, and limit what Cursor's GitHub app can access.

## How the polling works

Every poll interval, bugsbot lists open issues and PRs carrying each tracked label in each tracked
repo. It only takes a closer look at items whose `updated_at` changed:

- An **issue** needs work if bugsbot has never commented on it, or if a human has commented since
  bugsbot's last comment.
- A **PR** needs work if any human comment, review summary or unresolved review-thread comment
  hasn't had a direct bugsbot reply. Replies carry `<!-- bugsbot:reply-to:… -->` markers, so this
  state lives on GitHub itself.
- **Trusted authors (per repo, optional).** Under **Repos & labels**, give a repository trusted users
  and/or organisations. bugsbot then only works issues and PRs opened by those users, members of
  those orgs, or bugsbot's own GitHub account. Others show as *ignored*. It only responds to
  comments and reviews by those authors, and leaves everyone else's comments out of the agent's
  prompt. Org membership is checked through GitHub's API and cached for 10 minutes. Private
  memberships only count if bugsbot's GitHub token belongs to a member of that org.
- Repos without trusted authors use the global setting: by default, only comments from repo owners,
  members and collaborators count. Bot accounts are ignored unless listed as trusted users.
- A run is never repeated for the same trigger. A failed run waits for new activity or a manual
  **Re-run** from the UI.
- At most *N* agents run at once (configurable). The rest queue.

## Safety

- The agent's shell runs with no usable GitHub credentials: an empty `gh` config, no system git
  credential helpers, and SSH disabled. All authenticated git and GitHub traffic goes through the
  harness's own tools: `comment`, `reply_to_comment`, `push_branch` and `open_pull_request`. These
  sign the text and refuse to push to the default branch.
- A hook blocks `gh pr merge`, `/merge` API calls, `git push`, and `gh pr|issue` write commands.
  For Claude it's a `PreToolUse` hook; for Cursor's CLI, a `beforeShellExecution` hook. Cursor
  documents its hooks for the editor and cloud agents, but not yet explicitly for the CLI. Even
  without the hook, the agent has no GitHub credentials to merge or push with.
- None of this applies to Cursor Cloud Agent workers (see [Remote workers](#cursor-cloud-agents)).
- The GitHub client has no merge method.
- Issue and comment text is treated as untrusted input. Agents can search and fetch the web for
  research, and their shell has normal network access. They run arbitrary code in their checkout
  on the host, and the Anthropic or Cursor API key (if set) is in their environment. So run bugsbot on a
  machine or container you're comfortable giving that access to. The defences against prompt
  injection are who can trigger work: only labelled items, and by default only trusted
  commenters.
- Web searches are billed per search on top of tokens.
- Each run is capped by a time limit, and Claude runs also by max turns and a USD budget
  (all configurable).

## Development

```sh
uv run pytest
```
