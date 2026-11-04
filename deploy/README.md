# Deploying to Hetzner Cloud

The **Deploy** workflow (`.github/workflows/deploy.yml`) runs the tests, then deploys `main` to a
Hetzner Cloud server. It runs on every push to `main`, or by hand from **Actions → Deploy → Run
workflow**. It uses GitHub's standard `ubuntu-latest` runners, which are free for public repos.

The first run creates the server; after that, each run updates it in place. On each run it:

1. **Server.** Creates the Hetzner server if it doesn't exist (`deploy/hetzner.sh`), along with an
   SSH key and a firewall (inbound 22, 80 and 443 only). It never deletes or rebuilds the server,
   because bugsbot's state lives on its disk.
2. **Upload.** Uploads the commit to `/opt/bugsbot/app` and runs `deploy/provision.sh` as root, which:
   - installs git, the GitHub CLI, uv, Python, Claude Code and (with a domain) Caddy;
   - adds 2 GB of swap and turns off SSH password login;
   - creates a `bugsbot` user to run the harness and its agents. The app's code and virtualenv
     are root-owned, so agents can't modify the harness.
3. **Sign-ins.** Signs `gh` in as the bugsbot user with `BUGSBOT_GITHUB_TOKEN`; bugsbot uses that
   token via `gh auth token`.
4. **Claude Code config.** Pre-sets Claude Code's config so it doesn't ask first-run questions
   (onboarding, theme, "Enable Remote Control?"). It also trusts the bugsbot user's home folder.
   bugsbot trusts each remote worker's folder itself.
5. **bugsbot settings.** Applies your settings, repos and labels with `bugsbot configure`.
   Settings you pass overwrite what's there; repos and labels are only added, so ones added in the
   web UI stay.
6. **Service.** Installs a `bugsbot` systemd service, listening on 127.0.0.1:8765, and with a
   domain, Caddy in front of it with automatic HTTPS.

The server costs about €4–5/month: by default, the cheapest x86 type in the chosen location
with at least 2 vCPU and 4 GB RAM.

## Setup

### 1. Create a GitHub environment for the secrets

In the repo: **Settings → Environments → New environment**, named `production`. Under
**Deployment branches and tags**, choose **Selected branches** and add `main`.

Put the secrets below in this environment rather than at the repo level. bugsbot's agents push
branches to repos it tracks, and that can include this one. A workflow on any branch of the repo
can read repo-level secrets; environment secrets limited to `main` can't be read from other branches.

### 2. Secrets (in the `production` environment)

| Secret | Required | What it is |
|---|---|---|
| `HCLOUD_TOKEN` | yes | Hetzner Cloud console → your project → Security → API tokens → **Read & Write** |
| `DEPLOY_SSH_KEY` | yes | A private key for the deploy job (see below) |
| `BUGSBOT_GITHUB_TOKEN` | recommended | Token bugsbot uses for GitHub (see below) |
| `ANTHROPIC_API_KEY` | optional | Anthropic API key for agent runs. Without it, agent runs use Claude Code's login |
| `BUGSBOT_OAUTH_CLIENT_SECRET` | optional | GitHub OAuth App secret for signing in to the web UI |

Make the deploy key with:

```sh
ssh-keygen -t ed25519 -N "" -C bugsbot-deploy -f bugsbot-deploy
# paste the contents of bugsbot-deploy (the private key) into DEPLOY_SSH_KEY
```

For `BUGSBOT_GITHUB_TOKEN`, ideally create a separate GitHub account for the bot, so its comments
and PRs are clearly not yours. Then create a fine-grained personal access token on that account:

- **Repositories:** only the repos bugsbot should work on.
- **Permissions:** Contents, Issues and Pull requests read & write (Metadata read is added
  automatically).
- **Workflows:** don't grant it, so agents can't push changes to `.github/workflows`.

### 3. Variables (in the `production` environment, or the repo)

All optional:

| Variable | Example | |
|---|---|---|
| `BUGSBOT_DOMAIN` | `bugsbot.example.com` | Serve the UI over HTTPS here (Caddy + Let's Encrypt). If unset, the deploy uses the server's free [sslip.io](https://sslip.io) name, e.g. `203-0-113-7.sslip.io`. Set it to `none` for no public URL (SSH tunnel only) |
| `BUGSBOT_OAUTH_CLIENT_ID` | `Ov23li...` | GitHub OAuth App client ID |
| `BUGSBOT_ALLOWED_USERS` | `Briggsby,alice` | Who can sign in to the web UI |
| `BUGSBOT_ALLOWED_ORGS` | `macrocosmos` | Members of these orgs can sign in |
| `BUGSBOT_REPOS` | `Briggsby/bugsbot` | Repos to track (comma-separated) |
| `BUGSBOT_LABELS` | `bugsbot` | Labels to watch (comma-separated) |
| `BUGSBOT_SETTINGS_JSON` | `{"max_concurrent_agents": 1}` | Any other bugsbot settings |
| `ADMIN_SSH_PUBLIC_KEYS` | `ssh-ed25519 AAAA... you@laptop` | Your own public key(s), one per line, so you can SSH in as root |
| `HETZNER_LOCATION` | `nbg1` | Default `nbg1`; also `fsn1`, `hel1`, `ash`, `hil`, `sin` |
| `HETZNER_SERVER_TYPE` | `cx33` | Overrides the automatic choice |
| `HETZNER_SERVER_NAME` | `bugsbot` | Default `bugsbot` |
| `DEPLOY_KNOWN_HOSTS` | | The server's SSH host key; the first deploy prints what to put here |

**GitHub OAuth App** (Settings → Developer settings → OAuth Apps → New): use
`https://<domain>/` as the homepage and `https://<domain>/auth/github/callback` as the callback
URL. `<domain>` is `BUGSBOT_DOMAIN`, or the sslip.io name the deploy summary shows.

### 4. Deploy

Run **Actions → Deploy → Run workflow**. The run's summary page shows the server's IP and the
web UI's URL. Then:

1. **DNS:** with the default sslip.io name there's nothing to do. If you set your own
   `BUGSBOT_DOMAIN`, point its A record at the IP; Caddy fetches a certificate once DNS resolves.
2. **Host key:** set `DEPLOY_KNOWN_HOSTS` to the lines the log prints. Later deploys will then
   refuse to connect if the server's key changes.
3. **Sign Claude Code in.** This one step has to be done by hand, once:

   ```sh
   ssh -t root@<server-ip> sudo -iu bugsbot claude
   # in Claude Code, type /login, open the link it shows, and paste back the code
   ```

   Remote Control workers need a full claude.ai subscription login. As far as I know, the
   long-lived tokens from `claude setup-token` can't be used for Remote Control, so this login
   can't be done from CI. It persists on the server, and redeploys don't touch it. The deploy log
   shows whether Claude Code is signed in.

## Day to day

- **Redeploy:** merge to `main`, or run the workflow. State in `/home/bugsbot/.bugsbot` and the
  Claude Code login are kept.
- **Logs:** `ssh root@<ip> journalctl -u bugsbot -f`
- **A sign-in link without OAuth:** run
  `ssh root@<ip> sudo -u bugsbot /opt/bugsbot/app/.venv/bin/bugsbot login-link --home /home/bugsbot/.bugsbot --public-url https://<domain>`.
  Without a domain, open an SSH tunnel first
  (`ssh -L 8765:127.0.0.1:8765 root@<ip>`) and drop `--public-url`.
- **Tear down:** delete the server, firewall and SSH key in the Hetzner console. This deletes
  bugsbot's state.
