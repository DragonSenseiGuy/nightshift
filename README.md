# NightShift

**An overnight agent for macOS.** While you sleep it triages your email, plans your day
from your calendar and tasks, and does real coding work on your projects in a sandboxed
Linux container. In the morning you get one HTML briefing, a reviewable git branch per
project, and a queue of proposed actions — draft replies, branch merges — that do nothing
until you approve them.

Nothing is sent, merged or run without an explicit tap. That is the point of the design,
not a setting.

![The morning briefing](docs/screenshots/briefing.png)

*The morning briefing: email triaged by urgency with draft replies that have not been sent,
today's calendar with prep notes, task triage — and, at the top of the inbox, an email that
tried to hijack the agent reading it, summarised as what it is. [The rest of the
artifact](docs/screenshots/briefing-projects.png) is last night's project work, the agents'
own notes, and everything that went wrong.*

---

## Try it without setting anything up

Download the .dmg from the [latest release](../../releases/latest), drag NightShift.app to
Applications, then **right-click it and choose Open** the first time (the app is ad-hoc
signed, not notarised, so a double-click shows a scary error instead of an Open button).

A moon appears in the menu bar. With no NightShift daemon running, the app starts in **demo
mode**: it launches the copy of the daemon inside its own bundle, serving one canned night
built from the repo's test fixtures — a full briefing, three actions waiting in the approval
queue, and last night's agent transcripts to read. Every side effect is disarmed
(`app/demo.py`), so approving a draft reply in demo mode tells you what it *would* have sent
and sends nothing. Nothing in it is your mail; no key, no Google account and no Docker are
involved.

- **Requires macOS 14 or later, Apple Silicon.** The bundled daemon is an arm64 binary
  (`file dist/nightshiftd`); there is no Intel build.
- Demo state lives in `~/Library/Application Support/NightShift/demo` and is rebuilt from
  scratch on every launch. Deleting that folder removes every trace of the demo.
- From a checkout, the same thing without the app: `uv run python -m app demo`, then
  `open http://127.0.0.1:8412/health` — or point the SwiftUI client at it.

Running it *for real* — your inbox, your calendar, your repos — is the rest of this README.

## Why I built it

I kept starting my mornings the same way: an inbox that had filled up overnight, a calendar
I hadn't looked at, and a repo with a small, well-understood, tedious job sitting in it —
the flaky test, the dependency bump, the rename I'd been putting off. All of it was work a
model could do. None of it was work I'd let a model do unsupervised, because the failure
modes are "sent an email to a client" and "merged something into main", and both of those
are awake-hours problems even when the agent is a night owl.

So NightShift is built around that tension rather than around the agent. The agents run
while I'm asleep and can read plenty; what they cannot do is *act*. Every side effect they
want becomes a row in a queue with a sentence attached — "Approving SENDS this reply to
priya@acme-supply.example" — and it sits there until I click. The interesting engineering
turned out to be the boundaries, not the prompts: keeping the Gmail token in a host process
the container cannot reach, keeping email-derived text out of the one agent that has a
shell, making a prompt-injected email dead-end as escaped text in an HTML file.

## Tech stack

| Layer | What it is |
|---|---|
| Language | Python 3.13 (`uv` for everything), Swift 5.9 / SwiftUI for the Mac app |
| Agent loop | Hand-written (`runner/`) over an OpenAI-compatible endpoint — no agent SDK, one file (`runner/backends.py`) imports an LLM client |
| Data | Pydantic v2 models across every boundary; SQLite (stdlib `sqlite3`) for the approval queue, transcripts and snapshot metadata |
| Host services | FastAPI + uvicorn on three separate loopback surfaces (broker :8400, approvals :8401, UI :8402) |
| Sandbox | colima + Docker: an Ubuntu container on an internal network whose only exit is an egress-allowlist proxy |
| Google | Gmail / Calendar / Tasks read-only via `google-api-python-client`; OAuth tokens in the macOS Keychain, split into read and send slots |
| Scheduling | launchd (generated plist), `caffeinate`, `pmset`/`ioreg` power probing |
| Version control | `git` as a library of last resort — worktrees, `agent/*` branches, snapshot refs outside `refs/heads/`, a server-side `pre-receive` hook |
| Packaging | PyInstaller (`packaging/build_daemon.sh`) + a hand-assembled, ad-hoc-signed .app and .dmg (`packaging/build_app.sh`) |
| Tests | pytest — 474 tests, offline by construction (no test may reach a model) |

## How it works

One night, end to end:

1. **launchd fires** `python -m orchestrator run` at 03:00. Before anything else the
   orchestrator checks it is inside the scheduled window and on AC power, and holds a
   `caffeinate` assertion for the run. A refusal is written into the briefing — a night that
   did not happen must not look like a quiet one.
2. **The broker starts on the host**, bound to a Unix socket in a 0700 temp directory. It is
   the only thing with the Gmail token, and it is read-only: `/emails`, `/calendar`,
   `/tasks`. That socket is the *entire* interface the sandbox gets.
3. **The email agent and the calendar agent run**, each with its own tool allowlist. Email
   text and calendar text arrive as `PromptPart.tainted(...)`; the runner refuses to start
   an agent whose prompt carries a taint it has not declared. The models return JSON
   validated against a schema, and the facts — sender, subject, event time — are joined back
   from what the broker fetched, never from what the model wrote.
4. **The project agent works in a container.** A git worktree on a fresh `agent/<date>`
   branch is mounted read-write; there is no broker socket, no Google credential, no git
   remote and no network except the LLM through the proxy. It accepts *no* taint at all: an
   injected email cannot reach the one agent with a shell. It writes a JSON work report into
   a mounted directory, which the host reads back after the container exits.
5. **The host commits, diffs and pushes** the branch under a deploy key the container never
   sees, to a remote whose `pre-receive` hook rejects every ref outside `refs/heads/agent/`.
6. **Everything becomes one artifact.** `briefing.py` renders `out/briefing.html` with every
   model- and email-derived string escaped, including a Failures section that is always
   present. Draft replies and the branch merge are enqueued as **pending** approvals.
7. **You wake up.** A notification carrying counts and no untrusted text; a menu bar moon;
   a briefing; and a queue where each item states its exact side effect before the click
   that fires it. `approve()` is the only code path in the repo that reaches `send_emails`.

The three rules that shape all of it — secrets stay out of the sandbox, agent output is data
and never another agent's prompt, and every side effect waits for a human — are written up
in [Threat model](#threat-model) below and pinned by `tests/test_threat_model.py`.

---

## Setup from scratch

Roughly 20 minutes, most of it Google's consent screen. Every step is required for a real
run; you can stop after step 4 and rehearse the whole thing offline.

### 1. Prerequisites

```sh
brew install colima docker            # the sandbox runtime
brew install uv                       # or: curl -LsSf https://astral.sh/uv/install.sh | sh
brew install terminal-notifier        # optional: click-through wake-up notifications
```

You need **Python 3.13+**; `uv` fetches it for you. macOS 13+ with a Keychain (the OAuth
tokens live there) and Docker via colima — the orchestrator starts the VM on first run,
which takes a minute.

```sh
git clone <this repo> && cd NightShift
uv sync                 # runtime deps
uv sync --group app     # + rumps, for the menu bar (host-only, macOS-only)
```

### 2. A Google OAuth client

NightShift talks to your Gmail, Calendar and Tasks as *you*, through an OAuth client you
own. There is no NightShift server and no shared app.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) → **create a
   project** (any name).
2. **APIs & Services → Library** → enable **Gmail API**, **Google Calendar API** and
   **Google Tasks API**.
3. **APIs & Services → OAuth consent screen** → User type **External** → fill in the app
   name and your own email. While the app is in *Testing*, add your Google account under
   **Test users** — otherwise consent fails.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → application
   type **Desktop app**. (Desktop, not Web: the flow runs a loopback listener on
   `localhost:8765`.)
5. Copy the client ID and secret into a `.env` file in the repo root:

```sh
# .env — never commit this file (it is in .gitignore)
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxxxxx
OPENROUTER_API_KEY=sk-...                       # your LLM key (see step 3)
OPENROUTER_BASE_URL=https://ai.hackclub.com/proxy/v1   # optional; config wins over this
```

**The scopes NightShift asks for**, split across two independently-authorised credential
slots that are never merged:

| Slot | Scopes | Who uses it |
|---|---|---|
| `read` (`google-oauth:read`) | `gmail.readonly`, `calendar.readonly`, **optional** `tasks.readonly` | the broker — the only surface the sandbox can reach |
| `send` (`google-oauth:send`) | `gmail.send` | the host only, reached exclusively from an approved action |

Consent for each slot happens the first time it is used, or on demand:

```sh
uv run python google_auth.py authorise read    # opens a browser; tick Google Tasks too
uv run python google_auth.py authorise send
uv run python google_auth.py status read       # what the stored token actually grants
uv run python google_auth.py forget read       # drop it; the next use re-authorises
```

Tokens are stored in the **macOS Keychain** (service `NightShift`), never in a file. If you
decline the Tasks tickbox, the briefing says "Tasks unavailable" and the night still runs.

### 3. An LLM key

Any OpenAI-compatible endpoint works; the model slugs live in the config file and the key
lives in `.env`. Set `[llm].base_url` in `config/standing_instructions.toml` to your
provider and put the key in `OPENROUTER_API_KEY`. **Read
[The provider-side hard cap](#the-provider-side-hard-cap-you-must-do-this-yourself) before
pointing this at a card you own** — the in-repo budgets are the first line of defence, not
the last.

### 4. Your standing instructions

`config/standing_instructions.toml` is the one file you hand-edit. It carries your
priorities, writing style, per-agent model slugs and budgets, the schedule, retention, and
the projects the agent may work on. It is trusted host-authored data and is committed to
the repo, so **never put a secret in it** — the loader rejects unknown keys, so a typo is
loud rather than silent.

Rehearse the whole pipeline offline, against canned data, with no Google and no network:

```sh
uv run python main.py --mock --no-send --open       # → out/briefing.html in your browser
uv run python -m orchestrator run --mock --no-send --now --no-require-ac
```

### 5. Your first real run

```sh
uv run python api.py &                 # the broker on localhost:8400
curl -s "localhost:8400/emails?since=8h" | jq .
uv run python main.py                  # fetch → digest → email it to yourself
```

Then the full night, once, by hand:

```sh
uv run python -m orchestrator run --now
open out/briefing.html
```

### 6. Turn on the schedule and the menu bar

```sh
uv run python -m orchestrator schedule install    # see "Running it nightly" below
uv run python -m app                              # 🌙 in the menu bar
```

### What you still have to do by hand

Everything here is deliberate: each one either costs money, grants a capability, or lives
on a machine this repo cannot reach.

- [ ] Create the Google Cloud project, enable three APIs, add yourself as a test user.
- [ ] `uv run python google_auth.py authorise read` **and** `... authorise send` — two
      separate consents, on purpose.
- [ ] Re-consent the read slot if you are upgrading from an older checkout: `calendar.readonly`
      is a required scope now, so the old token is refused rather than silently used.
- [ ] Set a **provider-side spend cap** on the LLM key (below). Nothing in this repo can
      do it for you.
- [ ] `brew install terminal-notifier` if you want the wake-up notification to open the
      briefing when clicked. Without it the banner still appears, but a click opens the
      notifier instead.
- [ ] Before setting `push = true` on a project: create the deploy key **and** the
      server-side `agent/*` rule (below). The client-side check is not the lock that holds.
- [ ] Grant notification permission when macOS asks; check delivery with
      `uv run python -m orchestrator notify`.
- [ ] Keep the Mac plugged in, awake and logged in at the scheduled hour. A logged-out Mac
      has a locked Keychain and does not run the night — that is the security model, not a bug.

---

## Threat model

Two trust zones and one narrow bridge. The claims below are pinned by
`tests/test_threat_model.py`; where a claim does not hold as stated, it says so here.

**The host is trusted.** It holds every secret: the Google OAuth tokens (Keychain), the LLM
key (`.env`), the git deploy key. It runs the broker, the orchestrator, the approval queue
and the UI.

**The sandbox is untrusted.** An ephemeral Ubuntu container, torn down every run. It sits
on an *internal* Docker network with no internet; its only peer is an allowlisting egress
proxy that permits exactly one host (your LLM endpoint) on port 443 and denies everything
else by default. Verified live: `ai.hackclub.com` → 200, `example.com` and
`gmail.googleapis.com` → refused, a direct connection bypassing the proxy → no route.

### 1. Secrets never enter the sandbox — with one stated exception

What is *not* there: no Google token, no `GOOGLE_CLIENT_SECRET`, no `.env`, no Keychain
access, no deploy key, no approvals or transcripts database. Both container environments
are constructed field by field rather than inherited, the code staged into the project
container is a named allowlist (`config.py`, `models.py`, `project_step.py`, `runner/`) so
`send_emails.py`, `google_auth.py` and `gitops.py` are not even present to call, and the
summariser's worktree is built with `git ls-files --others --exclude-standard`, so an
ignored file like `.env` cannot ride in with your uncommitted work.

**The exception: `OPENROUTER_API_KEY` is passed into both containers.** The summariser and
the project agent call the model from *inside* the sandbox, so the key goes with them. This
is a genuine secret in an untrusted zone. What bounds it:

- the egress proxy allows exactly one destination, so the key can only be used against — or
  leaked to — the LLM endpoint it belongs to;
- spend is capped per agent (`max_cost_usd`, `max_steps`, `max_seconds`) and, if you set one
  up, at the provider;
- the container lives for one step of one night.

What it does **not** bound: a compromised project agent can spend your LLM budget, and can
send the contents of the repo it is working on to the LLM host. If that is not acceptable
for a given project, do not make it `active`. Removing the exception entirely would mean
proxying model calls through the host — a real change, not a doc fix, and it is not
implemented.

Two smaller notes for completeness: the sandbox image bakes NightShift's `*.py` files
(including `send_emails.py`) at build time — code, never credentials, and unreachable
without a token — and containers run as root inside the VM, which is the boundary that
matters, not the uid.

### 2. Summary-as-data: injection dead-ends in the briefing

There are two untrusted sources — **email** and **calendar/tasks** (anyone who can send you
an invite or share a list writes that text). Both are labelled at the moment they are
fetched (`runner/taint.py`), and the label travels with everything derived from them. An
agent that has not declared a taint cannot be handed data carrying it; the project agent —
the only one with a shell — declares **none**. There is deliberately no `declassify()`.

The two sources also stay in separate agents: `read_calendar` is not in the email agent's
tool registry and `read_emails` is not in the calendar agent's, so an injected event asking
for your inbox raises `ToolScopeError` and ends the run.

The models never emit markup. They return JSON validated against a schema and joined back
to the fetched records **by id**, so senders, subjects, times, titles and due dates always
come from Google rather than from the model. `briefing.py` escapes every model- and
email-derived string, and the briefing contains no `<script>`, no external stylesheet, font
or image, and no clickable link.

Both permanent injection fixtures (`fixtures/mock_emails.py`,
`fixtures/mock_calendar.py`) are carried through a whole night in `tests/test_end_to_end.py`
with the model stubbed to *fully comply* with them. They arrive escaped and inert; no
action is queued to the attacker's address; and feeding the result to the project agent
raises `TaintViolation`.

The banner is a rendering surface too: the wake-up notification carries counts and
host-authored words only, never a subject line.

### 3. Every side effect waits for a human

Agents propose; they never act. A proposal is a row in a durable SQLite queue and
`approve()` is the only code path that reaches `send_emails` or `git merge`. The read
broker (which the sandbox can talk to) cannot import either, and has no approve route — the
approval API is a separate process on a separate loopback port with no bridge into the
sandbox. Approving states the exact effect before the click that fires it.

### Deploy-key scoping

The push happens **on the host**, under an SSH key pinned with `IdentitiesOnly=yes` and
`IdentityAgent=none`, with one explicit refspec and no `--force`. `gitops.is_agent_ref`
checks the whole refname, so `agent/../main` is refused as well as `main`. The sandbox has
no route to a git remote at all and never sees the key.

That client-side check protects against *our* bugs. The lock that holds against a
compromised host is server-side: `hooks/pre-receive` (or a branch-protection rule) on the
remote, which the host cannot edit. Set it up before enabling `push`.

### What is out of scope

A compromised host, a malicious model *provider*, macOS itself, and anything you approve.
Rollback (`snapshots.py`) undoes a bad night's work in a repo; it cannot recall a sent
email or an already-pushed branch.

---

## The sandbox

A disposable Ubuntu container, driven from Python (docker-py) over
[colima](https://github.com/abiosoft/colima), into which a git **worktree** is mounted.
Tasks run isolated from the host and from your main checkout; the container and worktree
are torn down when the task exits.

```sh
uv run python -m sandbox.orchestrator "echo hello && uname -a && ls"
```

Flags: `--branch <ref>` (default: detached HEAD), `--image <tag>`, `--keep` (keep the
container for debugging). The container's exit code becomes the process exit code.

The broker is **not** a container: it is a host process (so it can reach the Keychain)
bound to a Unix socket in a private 0700 directory, mounted read-only into the sandbox.
The sandbox has no network route to it whatsoever.

## Nightly project work

The project agent works on a repo overnight and hands you a branch to review in the
morning. It runs in the sandbox with worktree-scoped `bash`/file tools, and it never
receives email or calendar data — that isolation is enforced by types, not convention
(`runner/taint.py`).

Declare a project in `config/standing_instructions.toml`:

```toml
[[projects]]
name = "myproject"
path = "~/code/myproject"
active = true
branch_prefix = "agent/"
remote = "origin"
push = true               # requires the deploy key + remote rule below
deploy_key = "~/.ssh/nightshift_agent"
merge_into = "main"
goals = ["Fix the flaky tests in tests/test_sync.py."]
```

```sh
uv run python nightly_project.py --project myproject --queue-merge
uv run python main.py --projects          # email digest + project work in one briefing
```

Each run creates `agent/YYYY-MM-DD` **before** the agent starts, so its work can only land
there. Your working copy is never touched: the agent gets a disposable worktree, `main` is
never checked out, and the branch survives the teardown. Afterwards the host commits
whatever is left, writes `out/diffs/<project>-<date>.diff`, pushes the branch, and queues a
**pending** `merge_branch` action.

Nothing merges on its own. Review the diff, then:

```sh
uv run python approvals.py                # the approval API on 127.0.0.1:8401
```

Approving is what merges, and it refuses unless `merge_into` is checked out and your tree
is clean.

### Deploy key setup (do this before setting `push = true`)

The push is host-side — the sandbox has no route to a git remote and never sees the key.
Two things you must set up yourself:

**1. A dedicated SSH key for the agent.**

```sh
ssh-keygen -t ed25519 -f ~/.ssh/nightshift_agent -C "nightshift agent"
```

Point `deploy_key` (or `$NIGHTSHIFT_DEPLOY_KEY`) at the private key and register the public
key with your remote. NightShift pins ssh to exactly this key
(`IdentitiesOnly=yes`, `IdentityAgent=none`) so a push cannot quietly succeed under your
own full-access key instead.

**2. A server-side rule restricting that key to `agent/*`.** This is the lock that actually
holds — the client-side check in `gitops.py` runs on the machine holding the key, so it
protects against our bugs, not against a compromised host.

- **Self-hosted / bare repo:** install the hook shipped in this repo.

  ```sh
  scp hooks/pre-receive you@server:/srv/git/myproject.git/hooks/pre-receive
  ssh you@server chmod +x /srv/git/myproject.git/hooks/pre-receive
  ```

  It rejects every ref outside `refs/heads/agent/`, plus deletions and tags. If humans push
  to the same remote, gate it on the authenticated user (see the comment in the file).

- **GitHub:** deploy keys cannot run a custom pre-receive hook on normal plans. Use a
  **branch/tag protection rule** (or a repository ruleset) that blocks the deploy key from
  pushing to anything but `agent/*`, and enable "Restrict who can push to matching
  branches" on `main`. Give the key write access only if such a rule is in place.

- **GitLab:** a **push rule** plus protected branches, restricting the deploy key to
  `agent/*`.

`tests/test_project_branches.py` verifies the hook against a real bare repo: `agent/*` is
accepted, `main` is rejected, and a control proves the same push succeeds without the hook
installed.

### Undoing a night

Every project is frozen by a git-based snapshot *before* its night starts — branch, HEAD,
staging area and untracked work, kept alive by a ref outside `refs/heads/`. The id is
printed in the briefing next to the work it undoes:

```sh
uv run python snapshots.py rollback <id> --dry-run   # print the plan, change nothing
uv run python snapshots.py rollback <id>             # asks first; safety-snapshots first
uv run python snapshots.py list | show <id> | take <repo> | prune
```

A rollback takes a safety snapshot of the state it is about to replace, so it is itself
undoable. It does **not** restore ignored files (`.env` and `node_modules/` survive, and
are never snapshotted), anything outside the repo, or side effects that already left the
machine.

## Running it nightly (launchd)

The nightly job is a **user** LaunchAgent (`gui/<uid>`), because the run needs your
Keychain and your colima VM. The plist is generated from this machine's facts rather than
committed — it must name an absolute `uv`, this repo and your home directory.

```sh
uv run python -m orchestrator schedule print     # inspect the plist first
uv run python -m orchestrator schedule install   # write it to ~/Library/LaunchAgents and load it
uv run python -m orchestrator schedule status    # installed? loaded? last exit code?
uv run python -m orchestrator schedule run-now   # launchctl kickstart — the same path as 3am
uv run python -m orchestrator schedule uninstall # bootout + delete
```

Set the time in `config/standing_instructions.toml` under `[schedule]` (`hour`, `minute`)
and re-run `install --force`; everything else in that section (`require_ac`, `caffeinate`,
`window_minutes`, `max_relaunches`, `projects`, `send`, `since`) is read at run time, so it
needs no reinstall.

Before enabling it, do a dry run and check the power guard:

```sh
uv run python -m orchestrator power
uv run python -m orchestrator run --mock --no-send --now --no-require-ac
```

Things worth knowing:

- **The machine must be plugged in, awake and logged in.** On battery the run refuses by
  default and says so in the briefing (`require_ac = false` overrides). Logged out, the
  Keychain is locked and a user agent does not fire at all.
- **launchd may start the job at odd times** — at bootstrap, and when a missed 3am interval
  catches up on wake. The run exits immediately unless it is inside `window_minutes` of the
  scheduled time; `--now` overrides.
- **Logs:** `~/Library/Logs/NightShift/nightly.{out,err}.log`. The briefing is
  `out/briefing.html`, and a crash or a refusal shows up in its Failures section.
- Only a *crashed* run is relaunched (`KeepAlive = {SuccessfulExit: false}`), at most
  `max_relaunches` times per night.

## Notifications

When a night finishes — successfully, with failures, refused or crashed — NightShift posts
one banner. It carries counts and host-authored words only ("5 emails, 2 need a reply ·
3 events · 1 failure"), never a subject line or a sentence an agent wrote.

```sh
uv run python -m orchestrator notify     # post a test banner and check permissions
```

`terminal-notifier` is used when installed, because it is the only backend that can **open
the briefing when clicked**; without it the built-in `osascript` banner is posted and a
click opens Script Editor instead. Grant notification permission when macOS asks — the API
returns success whether or not the banner was actually presented, so a silent failure looks
like nothing at all. `[notifications] enabled = false` turns it off.

## The menu bar app

```sh
uv sync --group app        # rumps — host-only, macOS-only, never in the sandbox image
uv run python -m app       # 🌙 appears in the menu bar
```

The icon says what is going on: idle, a run in progress, the last run failed, or actions
waiting for approval. From the menu you can **Run now** (spawned in the background — the
menu stays responsive and the run survives quitting the app; progress goes to
`~/Library/Logs/NightShift/run-now.log`), **Open last briefing** (`out/briefing.html` in
your browser), and decide each pending approval.

Approving is the only thing in NightShift that sends mail or merges a branch, so every
item opens a dialog that says exactly what will happen — which address a reply leaves for,
which branch merges into which — with Approve, Reject and Cancel as separate buttons. If
you are on battery within two hours of the scheduled start, the menu says tonight's run
will be skipped.

## The native app (SwiftUI)

The rumps menu bar is the v1 UI; `app/NightShiftUI/` is the v2 one — a real macOS menu bar
app with a proper approval-review window, a transcript browser and native notifications.
It talks to a small localhost daemon rather than importing anything, so all the logic still
lives in one place (`app/service.py`).

```sh
uv run python -m app serve                # the daemon on 127.0.0.1:8402 (host-only)
cd app/NightShiftUI && ./build.sh --run   # build NightShift.app and launch it
./build.sh --install                      # ... or put it in /Applications
```

Run the daemon first — the client polls it every five seconds and says so plainly when it
is not there. Both are needed: the app is a *client*, and quitting it never affects a
running night.

What the native client adds over the menu:

- **a review window for approvals.** The effect sentence sits at the top of the detail
  pane in full, a draft written from untrusted email carries a standing warning, and the
  body is shown as plain monospaced text — never rendered, never interpreted. Approve is a
  second, explicit confirmation that repeats the same sentence.
- **a transcript viewer.** Nights → agent runs → the run, with each tool call expandable
  and its taint labels attached; the toolbar button fetches the same full replay
  `uv run python transcripts.py replay <id>` prints.
- **native notifications** when a night ends and when approvals appear. Counts and
  host-authored words only — never a subject line — for the same reason the daemon's
  banner is: a notification is drawn by the system, outside every escape in `briefing.py`.

**About the daemon's port.** It is loopback-only and token-gated: it writes a random token
to `~/Library/Application Support/NightShift/ui-token` (0600) and the client reads the same
file. Loopback is not an authentication boundary — anything running on the Mac can reach
127.0.0.1, and this surface can send mail — so every route but `/health` requires the token.
It is deliberately a *different* process from the broker on :8400, which is the only surface
the sandbox can reach and which has no approve route at all.

Two things macOS wants: the build script ad-hoc signs the bundle (a stable identity, so a
granted notification permission survives a rebuild), and notifications only work when the
app is launched as a bundle — running the bare SwiftPM binary logs a line saying they are
off and carries on.

### Building the release .dmg

```sh
./packaging/build_app.sh          # dist/NightShift-<version>.dmg
./packaging/build_app.sh --no-dmg # stop at app/NightShiftUI/.build/NightShift.app
./packaging/build_daemon.sh       # just dist/nightshiftd
```

Three steps, each explained in the script that performs it:

1. **`packaging/build_daemon.sh`** freezes `python -m app serve` into a single-file
   `dist/nightshiftd` with PyInstaller, so the app works on a Mac with no Python, no `uv`
   and no checkout. PyInstaller does not cross-compile — the binary's architecture is the
   *build interpreter's* architecture — and this repo's `uv` is an x86_64 build, so the
   script creates and uses a native arm64 venv (`build/arm64venv`, from Homebrew's
   `python3`) rather than quietly shipping an Intel helper an arm64 app cannot spawn.
   `file dist/nightshiftd` is the check that matters after touching it.
2. **`app/NightShiftUI/build.sh`** builds the SwiftUI binary, assembles the bundle around
   it, copies the daemon into `Contents/Resources`, and ad-hoc signs the helper *then* the
   bundle (signing seals the contents, so that order is load-bearing). With no
   `dist/nightshiftd` present it still produces the developer build — an app that drives
   your own `python -m app serve`.
3. **`hdiutil`** wraps it in a .dmg with an `/Applications` symlink and a plain-text
   "read me first" beside it, because the person hitting the unsigned-app error is looking
   at a mounted disk image, not at this file.

The output has **no Apple Developer signature** — there is no paid certificate behind this
project — so the first launch is a right-click → Open. Everything after that is a normal
double-click. If you move the app while a demo daemon is running, quit and relaunch: the
supervisor starts the copy inside the bundle it was launched from.

## Budgets, spend caps and run history

Every agent run is bounded by three caps from `config/standing_instructions.toml`, all
enforced in `AgentRunner.run`:

```toml
[agents.project_agent]
max_steps    = 40      # loop iterations (no off switch)
max_cost_usd = 5.00    # spend at [pricing] rates; 0 = no cap
max_seconds  = 3600    # wall clock; 0 = no cap
```

Whichever is reached first ends the run **cleanly**: the partial work, the tool-call
transcript and a stop reason (`step_limit` / `cost_limit` / `time_limit`) are all kept, and
the briefing says the goal was cut short. Caps are checked *before* the next model call, so
a blown budget never buys another one.

Costs come from `[pricing]`, which lists USD per million tokens per model slug. **A slug
that is not listed is billed at `unknown_*`, which defaults to frontier prices** — pricing
an unknown model at zero would silently switch the cost cap off for exactly the slug nobody
re-checked. If your endpoint does not return `usage`, tokens are estimated from the bytes on
the wire and the stored run is flagged `estimated`, so an unmetered provider still spends
against the cap.

### The provider-side hard cap (you must do this yourself)

The in-repo caps protect you from a *runaway agent*. They cannot protect you from a bug in
the code that enforces them, so the second line of defence is a key your provider will not
let you exceed. That is a dashboard action; nothing in this repo can do it for you:

- **On OpenRouter** (`base_url = "https://openrouter.ai/api/v1"`): openrouter.ai → Keys →
  *Create key* with a **credit limit**, and use it as `OPENROUTER_API_KEY` in `.env`. A
  provisioned key with a hard limit stops spending at the provider, whatever this code does.
- **On the Hack Club AI proxy** (`https://ai.hackclub.com/proxy/v1`, which is what
  `OPENROUTER_BASE_URL` points at in this checkout despite the variable's name): there is no
  per-key spend dashboard to configure. The proxy owns the upstream credential and its own
  limits, so on this endpoint the caps above plus `transcripts.py spend` are what you have.
  Switch `base_url` to openrouter.ai if you want a hard cap you control.

Either way, `OPENROUTER_API_KEY` is read from `.env` on the host and passed to the sandbox
as an environment variable for the duration of one container; it is never written to config.
See [Threat model](#1-secrets-never-enter-the-sandbox--with-one-stated-exception) for what
that does and does not bound.

### What ran last night

```sh
uv run python transcripts.py nights            # run history: outcome, failures, cost
uv run python transcripts.py list              # recent agent runs
uv run python transcripts.py replay <id>       # the full transcript, step by step
uv run python transcripts.py spend --days 30   # what the agents have cost (our figures)
uv run python transcripts.py prune             # apply [retention].transcript_days
```

The briefing prints the transcript id and the replay command for each project card — an
emailed HTML file cannot host a button, so it hands you the command instead.

Transcripts live in `~/Library/Application Support/NightShift/transcripts.db` and contain
email-derived text. They are storage and display only: nothing reads a stored run back into
an agent's prompt, the taint labels travel with the row, and a replay says so at the top.
One row per agent run per night grows without bound, so the nightly run prunes anything
older than `[retention].transcript_days` (30 by default; `0` keeps everything).

## Tests

```sh
uv run pytest                                          # the whole suite: offline, no spend
NIGHTSHIFT_SANDBOX_TESTS=1 uv run pytest -m sandbox    # boots a real container (needs colima)
NIGHTSHIFT_LAUNCHD_TESTS=1 uv run pytest -m launchd    # loads a job into your launchd
NIGHTSHIFT_GUI_TESTS=1 uv run pytest -m gui            # builds real AppKit menu items
```

`uv run pytest` reaches no network and calls no model: `tests/conftest.py` replaces the one
place an LLM client is constructed with the deterministic stub in `tests/offline_llm.py`,
and overwrites the API key. The three marked suites above are opt-in because each one
touches something real — a container, your launchd domain, or AppKit.

`tests/test_end_to_end.py` runs a whole night on `--mock` data and asserts on the finished
artifact; `tests/test_threat_model.py` pins the claims in [Threat model](#threat-model).
