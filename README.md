# Research Threads

A local dashboard for the research threads you choose to track — each one a
Claude Code or Codex session inside an Emacs vterm. You start a thread by
telling your agent to track it; from then on the session is followed
automatically, keeps its full history after it closes, and the agent
maintains the thread's objective, current status, notes and plots.

## The model

Threads are **explicitly started, automatically followed**:

```
you:    "track this as a research thread about value-function collapse"
agent:  rt start value-fn-collapse -o "Test whether the value fn explains collapse"
```

That one command registers the vterm as a thread. The server then follows it
on its own: whether it is open, working, waiting for your input, or closed
(via `ps` + `CLAUDE_VTERM_NAME` + the hook state files in `~/.claude/state/`).
Ordinary coding sessions are never tracked — no registration, no thread.

Each thread carries:

- **Objective** — one sentence stating the research question (`rt start -o` /
  `rt objective`).
- **Current status** — a living section the agent replaces with `rt update`
  after significant progress: where the investigation stands + outstanding
  TODOs. The `research-dashboard` skill makes agents keep this fresh.
- **Notes / plots / links** — an append-only timeline (`rt note`, `rt plot`,
  `rt link`), written by agents and by you.

## The three views

- **Web app** — http://localhost:7878 · ivory light theme. Collapsible left
  sidebar lists active threads and history; the center shows each active
  thread as a floating bubble — gently drifting, breathing while the agent
  works, rippling when it needs you. Click anything for the detail panel
  (objective, current status, notes, plots, pin/archive/rename, jump to
  Emacs). `/` filters; `#t<id>` deep-links.
- **Emacs dashboard** — `C-c r` (or `M-x research-threads`). `RET` jumps to
  the vterm (offers to reopen closed threads), `n` adds a note, `o` opens the
  thread on the web, `a`/`p` archive/pin, `g` refreshes. Auto-refreshes while
  visible.
- **`rt` CLI** — everything above, plus `rt threads`, `rt whoami`, `rt open`.

## Install / update

```bash
./install.sh          # idempotent; re-run after git pull
./install.sh --uninstall
```

Installs: `rt` in `~/.local/bin`, the `research-dashboard` skill for both
Claude Code and Codex, Codex status hooks (backs up `~/.codex/hooks.json`;
Codex may ask to re-trust hooks), a launchd agent that starts the server at
login, and a guarded block in `~/.emacs.d/init.el`.

The server is self-managing (launchd at login; `rt`, Emacs, and the skill
start it on demand), binds 127.0.0.1 only, and stores everything in
`~/.research-threads/` (SQLite + copied plot files). Logs:
`~/.research-threads/server.log`.

## Layout

```
server.py                     zero-dependency server: tracking, SQLite, API, SSE
web/                          the web app (vanilla HTML/CSS/JS, no build step)
bin/rt                        CLI used by agents and humans
emacs/research-threads.el     the Emacs dashboard
skills/research-dashboard/    SKILL.md for Claude Code and Codex
launchd/                      launch-at-login plist template
install.sh                    idempotent installer / uninstaller
```

## API

```
GET  /api/state                     all threads with live status
GET  /api/threads/<id>              one thread: notes, sessions, status history
GET  /api/events                    SSE stream of state snapshots
POST /api/register                  {name, objective?, vterm?|cwd?}   (rt start)
POST /api/objective                 {text, vterm?|cwd?|thread_id?}
POST /api/status                    {text, vterm?|cwd?|thread_id?}    (rt update)
POST /api/notes                     {text, kind?, author?, vterm?|cwd?|thread_id?}
POST /api/plots                     {path, caption?, vterm?|cwd?|thread_id?}
POST /api/threads/<id>/<action>     archive|unarchive|pin|unpin|rename|focus
```

`focus` switches Emacs to the thread's vterm via `emacsclient` (the Emacs
dashboard enables `server-start` so this works).
