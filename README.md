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
2. **Settings**: add your Anthropic API key. If you leave it blank, Claude Code's own login is used.
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

## Remote workers

**Remote workers** in the web UI starts `claude remote-control` on the bugsbot host, in a fresh
checkout of a tracked repo (or an empty scratch folder). You talk to the worker from the Claude app,
not from bugsbot. bugsbot shows an **Open in Claude** link once the session connects, records its
start-up output and duration, and can stop it.

- The host must be logged in to Claude Code (`claude` then `/login`) with a Claude subscription
  account. The worker appears in **that account's** Claude app, and only that account can open it.
  Usage counts against that subscription, so bugsbot doesn't know the worker's cost.
- **Branch** is optional. An existing branch is checked out; a new name is created from the default
  branch. If it's blank, the worker starts on the default branch (which it can't push to) and
  creates a branch named for the task once you've told it what to do.
- bugsbot accepts Claude Code's "trust this folder?" prompt for the worker's own checkout.
- The worker gets bugsbot's rules through project-local Claude Code config in the checkout,
  hidden from git: the no-merge/no-push hook, the signed `push_branch`/`open_pull_request`/
  `comment` tools, and the hard rules in `CLAUDE.local.md`.
- The session link is read from Claude Code's local session records, which aren't a public
  format. If it's missing, look for the worker by name in the Claude app's Code section.

## How the polling works

Every poll interval, bugsbot lists open issues and PRs carrying each tracked label in each tracked
repo. It only takes a closer look at items whose `updated_at` changed:

- An **issue** needs work if bugsbot has never commented on it, or if a human has commented since
  bugsbot's last comment.
- A **PR** needs work if any human comment, review summary or unresolved review-thread comment
  hasn't had a direct bugsbot reply. Replies carry `<!-- bugsbot:reply-to:… -->` markers, so this
  state lives on GitHub itself.
- By default, only comments from repo owners, members and collaborators count (configurable). Bot
  accounts are ignored.
- A run is never repeated for the same trigger. A failed run waits for new activity or a manual
  **Re-run** from the UI.
- At most *N* agents run at once (configurable). The rest queue.

## Safety

- The agent's shell runs with no usable GitHub credentials: an empty `gh` config, no system git
  credential helpers, and SSH disabled. All authenticated git and GitHub traffic goes through the
  harness's own tools: `comment`, `reply_to_comment`, `push_branch` and `open_pull_request`. These
  sign the text and refuse to push to the default branch.
- A `PreToolUse` hook blocks `gh pr merge`, `/merge` API calls, `git push`, and `gh pr|issue`
  write commands.
- The GitHub client has no merge method.
- Issue and comment text is treated as untrusted input. Agents can search and fetch the web for
  research, and their shell has normal network access. They run arbitrary code in their checkout
  on the host, and the Anthropic API key (if set) is in their environment. So run bugsbot on a
  machine or container you're comfortable giving that access to. The defences against prompt
  injection are who can trigger work: only labelled items, and by default only trusted
  commenters.
- Web searches are billed per search on top of tokens.
- Each run is capped by max turns and a USD budget (configurable).

## Development

```sh
uv run pytest
```
