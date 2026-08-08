#!/usr/bin/env python3
"""Research Threads — local dashboard server.

Auto-detects Claude Code / Codex sessions running inside Emacs vterms,
persists them as research threads with notes, plots and status history,
and serves a web dashboard plus a small JSON API for agents.

Zero dependencies: Python 3 stdlib only. Run with `python3 server.py`.

Detection strategy (macOS):
  - `ps` finds interactive `claude` / `codex` processes (they own a tty).
  - Each process's environment (via `ps eww`) carries CLAUDE_VTERM_NAME,
    exported by Emacs when the vterm was launched — that names the thread.
  - `lsof` gives the working directory (the project the thread is about).
  - `~/.claude/state/<vterm>.json`, written by Claude Code / Codex hooks,
    provides live sub-status: working / idle / needs-permission /
    needs-attention.
"""

import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration

PORT = int(os.environ.get("RESEARCH_THREADS_PORT", "7878"))
HOME = Path.home()
DATA_DIR = Path(os.environ.get("RESEARCH_THREADS_DATA", HOME / ".research-threads"))
PLOTS_DIR = DATA_DIR / "plots"
DB_PATH = DATA_DIR / "threads.sqlite"
LOG_PATH = DATA_DIR / "server.log"
STATE_DIR = HOME / ".claude" / "state"
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"
WEB_DIR = Path(__file__).resolve().parent / "web"

POLL_INTERVAL = 2.5          # seconds between process scans
CWD_REFRESH_EVERY = 12       # re-run lsof on live pids every N polls
CLOSED_GRACE_POLLS = 2       # missed polls before a session counts as closed

AGENT_NAMES = {"claude", "codex"}
CLAUDE_NONINTERACTIVE = {
    "daemon", "bg-pty-host", "bg-spare", "mcp", "config", "doctor",
    "update", "install", "migrate-installer", "setup-token", "api",
}
CODEX_NONINTERACTIVE = {"exec", "login", "logout", "mcp", "proto", "apply", "completion"}
EXCLUDE_PATTERNS = ("crashpad", "ChatGPT.app", "--bg-pty-host", "--bg-spare", "bg-pty-host")

PLOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".html"}

# Sub-status values, in the order they matter to a human scanning the board.
STATUSES = ("needs-permission", "needs-attention", "working", "idle", "closed")


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5_000_000:
            LOG_PATH.write_text("")
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


# ---------------------------------------------------------------------------
# Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,          -- 'vterm:<name>' or 'cwd:<path>'
    name TEXT NOT NULL,
    agent TEXT,                        -- 'claude' | 'codex' | NULL (unknown)
    cwd TEXT,
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    pid INTEGER NOT NULL,
    agent TEXT,
    cwd TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    ts INTEGER NOT NULL,
    author TEXT,                       -- 'max' | 'claude' | 'codex' | ...
    kind TEXT NOT NULL DEFAULT 'note', -- 'note' | 'plot' | 'link'
    text TEXT,
    path TEXT                          -- for plots: path relative to plots dir
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    ts INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_thread ON notes(thread_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id, ts);
CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_id, started_at);
"""


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            for stmt in (
                "ALTER TABLE threads ADD COLUMN objective TEXT",
                "ALTER TABLE threads ADD COLUMN status_text TEXT",
                "ALTER TABLE threads ADD COLUMN status_updated_at INTEGER",
                "ALTER TABLE threads ADD COLUMN unread_at INTEGER",
            ):
                try:
                    self._db.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def query(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    # -- threads ------------------------------------------------------------

    def upsert_thread(self, key, name, agent, cwd, ts):
        rows = self.query("SELECT * FROM threads WHERE key = ?", (key,))
        if rows:
            t = rows[0]
            self.execute(
                "UPDATE threads SET agent = COALESCE(?, agent),"
                " cwd = COALESCE(?, cwd), last_active_at = MAX(last_active_at, ?)"
                " WHERE id = ?",
                (agent, cwd, ts, t["id"]),
            )
            return t["id"], False
        tid = self.execute(
            "INSERT INTO threads (key, name, agent, cwd, created_at, last_active_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (key, name, agent, cwd, ts, ts),
        )
        return tid, True

    def touch(self, thread_id, ts):
        self.execute(
            "UPDATE threads SET last_active_at = MAX(last_active_at, ?) WHERE id = ?",
            (ts, thread_id),
        )

    def latest_status(self, thread_id):
        rows = self.query(
            "SELECT status FROM events WHERE thread_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (thread_id,),
        )
        return rows[0]["status"] if rows else None

    def record_status(self, thread_id, status, ts):
        """Append a status event iff it differs from the latest one."""
        if self.latest_status(thread_id) != status:
            self.execute(
                "INSERT INTO events (thread_id, ts, status) VALUES (?, ?, ?)",
                (thread_id, ts, status),
            )
            return True
        return False

    def set_unread(self, thread_id, ts):
        """Flag (ts) or clear (None) an agent message the user hasn't seen."""
        self.execute("UPDATE threads SET unread_at = ? WHERE id = ?", (ts, thread_id))


# ---------------------------------------------------------------------------
# Process detection

def _run(cmd, timeout=10):
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return out.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _parse_lstart(text):
    try:
        return int(time.mktime(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return int(time.time())


def scan_agent_processes():
    """Return {pid: {...}} for interactive claude/codex processes."""
    out = _run(["ps", "-axo", "pid=,ppid=,pcpu=,tty=,command="])
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid_s, ppid_s, pcpu_s, tty, command = parts
        if tty == "??":
            continue  # not attached to a terminal -> not an interactive session
        if any(p in command for p in EXCLUDE_PATTERNS):
            continue
        tokens = command.split()
        base = os.path.basename(tokens[0])
        if base not in AGENT_NAMES:
            continue
        sub = next((t for t in tokens[1:] if not t.startswith("-")), None)
        if base == "claude" and sub in CLAUDE_NONINTERACTIVE:
            continue
        if base == "codex" and sub in CODEX_NONINTERACTIVE:
            continue
        try:
            procs[int(pid_s)] = {
                "pid": int(pid_s),
                "ppid": int(ppid_s),
                "pcpu": float(pcpu_s),
                "tty": tty,
                "agent": base,
            }
        except ValueError:
            continue
    return procs


def read_proc_env(pid):
    """Best-effort read of CLAUDE_VTERM_NAME and PWD from a process env."""
    out = _run(["ps", "eww", "-p", str(pid), "-o", "command="])
    env = {}
    m = re.search(r"\bCLAUDE_VTERM_NAME=(\S+)", out)
    if m:
        env["CLAUDE_VTERM_NAME"] = m.group(1)
    m = re.search(r"\bPWD=(\S+)", out)
    if m:
        env["PWD"] = m.group(1)
    return env


def read_proc_cwd(pid):
    out = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def read_proc_start(pid):
    out = _run(["ps", "-p", str(pid), "-o", "lstart="]).strip()
    return _parse_lstart(out) if out else int(time.time())


def subtree_cpu(pid, procs_all):
    """Total %cpu of pid and its descendants (fallback activity signal)."""
    children = {}
    for p in procs_all:
        children.setdefault(p["ppid"], []).append(p)
    total, stack, seen = 0.0, [pid], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for p in procs_all:
            if p["pid"] == cur:
                total += p["pcpu"]
        stack.extend(c["pid"] for c in children.get(cur, ()))
    return total


def scan_all_processes():
    out = _run(["ps", "-axo", "pid=,ppid=,pcpu="])
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "pcpu": float(parts[2])})
            except ValueError:
                pass
    return rows


def read_state_file(vterm_name):
    """Read ~/.claude/state/<name>.json -> (status, ts) or (None, 0)."""
    f = STATE_DIR / ("%s.json" % vterm_name)
    try:
        data = json.loads(f.read_text())
        return data.get("state"), int(data.get("ts", 0))
    except (OSError, ValueError):
        return None, 0


# ---------------------------------------------------------------------------
# Background work: subagents, monitors, background commands
#
# Claude Code exposes no count of these, so we reconstruct it from the session
# transcript. ~/.claude/sessions/<pid>.json maps a live agent process to its
# session; the transcript JSONL then tells the whole story: a tool result
# announces a task id when work is dispatched, and a <task-notification>
# carrying a <status> marks that task finished. Transcripts are tailed from a
# remembered offset, so a poll only ever reads what was appended.
#
# Only Claude Code writes these; Codex threads simply report nothing.

TASK_ID_RE = {
    "agents": re.compile(r"agentId: (\w+)"),
    "monitors": re.compile(r"Monitor started \(task ([A-Za-z0-9]+)"),
    "commands": re.compile(r"background(?: with ID:|\s*\(ID:)\s*([A-Za-z0-9]+)"),
}
OUTPUT_PATH_RE = re.compile(r"(/\S+/tasks/[A-Za-z0-9]+\.output)")
NOTIFICATION_RE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
NOTIFICATION_TASK_RE = re.compile(r"<task-id>\s*(\S+?)\s*</task-id>")
# A monitor reports progress without ever setting <status>; the only thing that
# ends it from the outside is its own timeout notice.
NOTIFICATION_DONE_RE = re.compile(r"<status>|Monitor timed out")

# Not every task announces its end: a monitor stops emitting without a final
# word, and work killed by an interrupt says nothing at all. Each task does
# write to <scratchpad>/<session>/tasks/<task-id>.output as it runs, though —
# an agent's is its own transcript, a monitor's grows every poll — so a
# recently written file means live work.
#
# A quiet file proves nothing on its own: a monitor that sleeps between polls,
# or a command waiting on a slow job, can go hours without a word. Monitors and
# background commands are shell loops, though, so the process table answers
# directly — their command line is still there, verbatim, for as long as they
# run. Subagents have no process of their own and fall back to asking whether
# anyone still holds the output file open, which costs an lsof.
TASK_STALE_AFTER = 900   # seconds of silence before a task needs a second look
TASK_RECHECK_EVERY = 30  # seconds between lsof checks of the same task

# `ps` renders newlines and tabs in an argument as \012 / \011, and the shell
# wrapper Claude Code puts around a command re-quotes every quote inside it.
# Dropping those, and all whitespace, leaves something that survives the round
# trip and can be looked for in the process table verbatim.
PS_ESCAPE_RE = re.compile(r"\\0[0-9]{2}")
CMD_NOISE_RE = re.compile(r"""[\s'"\\]+""")
# Long enough that two monitors watching different jobs never collide, short
# enough to stay clear of any limit on how much of a command line `ps` reports.
CMD_NEEDLE = 1000


def normalize_cmd(text):
    return CMD_NOISE_RE.sub("", PS_ESCAPE_RE.sub("", text))


def scan_command_lines():
    """Every running command line, normalised into one searchable blob."""
    return normalize_cmd(_run(["ps", "-axww", "-o", "command="]))


def find_session(pid):
    """Transcript JSONL and task-output directory of a running agent process."""
    try:
        session = json.loads((SESSIONS_DIR / ("%d.json" % pid)).read_text())
    except (OSError, ValueError):
        return None, None
    sid, cwd = session.get("sessionId"), session.get("cwd")
    if not sid:
        return None, None
    transcript = None
    if cwd:
        # Project directories are the cwd with every non-alphanumeric
        # character replaced by a dash.
        guess = PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", cwd) / ("%s.jsonl" % sid)
        if guess.is_file():
            transcript = guess
    if transcript is None:
        transcript = next(iter(PROJECTS_DIR.glob("*/%s.jsonl" % sid)), None)
    scratch = Path("/private/tmp") / ("claude-%d" % os.getuid())
    tasks = next(iter(scratch.glob("*/%s/tasks" % sid)), None)
    return transcript, tasks


def tool_kind(block):
    """Which flavour of background work a tool_use block dispatches, if any."""
    name = block.get("name")
    args = block.get("input") or {}
    if name == "Agent":
        return "agents"
    if name == "Monitor":
        return "monitors"
    if name == "Bash" and args.get("run_in_background"):
        return "commands"
    return None


class BackgroundWork:
    """Live per-pid counts of subagents, monitors and background commands."""

    KINDS = ("agents", "monitors", "commands")

    def __init__(self):
        self._state = {}     # pid -> parser state
        self._commands = ""  # every running command line, as of the last refresh

    def counts(self, pid):
        st = self._state.get(pid)
        if st is None:
            return {}
        out = {k: 0 for k in self.KINDS}
        for task, kind in list(st["active"].items()):
            if self._running(st, task):
                out[kind] += 1
            else:
                del st["active"][task]
                st["outputs"].pop(task, None)
                st["checked"].pop(task, None)
                st["cmds"].pop(task, None)
        out["agents"] += len(st["blocking"])  # foreground agents, still running
        return out

    def _running(self, st, task):
        """Is this task still writing, still on the process table, or still
        holding its log open?"""
        path = st["outputs"].get(task)
        if path is None:
            if st["tasks"] is None:
                return False  # can't see its output — can't claim it is running
            path = st["tasks"] / ("%s.output" % task)
        now = time.time()
        try:
            if now - path.stat().st_mtime < TASK_STALE_AFTER:
                return True
        except OSError:
            return False
        needle = st["cmds"].get(task)
        if needle:
            return needle in self._commands
        when, verdict = st["checked"].get(task, (0, True))
        if now - when < TASK_RECHECK_EVERY:
            return verdict
        verdict = bool(_run(["lsof", "-t", str(path)], timeout=5).strip())
        st["checked"][task] = (now, verdict)
        return verdict

    def refresh(self, pids):
        self._commands = scan_command_lines()
        for pid in list(self._state):
            if pid not in pids:
                del self._state[pid]
        for pid in pids:
            st = self._state.get(pid)
            if st is None:
                path, tasks = find_session(pid)
                if path is None:
                    continue
                st = self._state[pid] = {"path": path, "tasks": tasks, "offset": 0}
                self._reset(st)
            elif st["tasks"] is None:
                st["tasks"] = find_session(pid)[1]  # created on first task
            try:
                self._tail(st)
            except OSError:
                pass

    @staticmethod
    def _reset(st):
        st["offset"] = 0
        st["dispatched"] = {}  # tool_use id -> (kind, command), until its result
        st["active"] = {}      # task id -> kind
        st["outputs"] = {}     # task id -> the file it writes to, when stated
        st["cmds"] = {}        # task id -> what to look for in the process table
        st["checked"] = {}     # task id -> (when, is-anyone-holding-it-open)
        st["blocking"] = set() # tool_use ids of agents the thread is waiting on

    def _tail(self, st):
        with open(st["path"], "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < st["offset"]:
                self._reset(st)  # truncated or replaced — start over
            f.seek(st["offset"])
            raw = f.read()
        cut = raw.rfind(b"\n") + 1
        if not cut:
            return
        st["offset"] += cut
        for line in raw[:cut].decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                self._consume(st, json.loads(line), line)
            except ValueError:
                continue

    def _consume(self, st, entry, line):
        if entry.get("isSidechain"):
            return  # a subagent's own tool calls are not the thread's work
        if entry.get("type") not in ("assistant", "user"):
            # Harness bookkeeping — queued notifications, attachments. The
            # shape varies by CLI version, so scan the whole record.
            self._finish(st, line)
            return
        from_user = entry.get("type") == "user"
        if not from_user:
            # The thread cannot speak again while it waits on a foreground
            # agent, so anything still pending here was cut short.
            st["blocking"].clear()
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            if from_user:
                self._finish(st, content)
            return
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                kind = tool_kind(block)
                if kind:
                    command = (block.get("input") or {}).get("command") or ""
                    st["dispatched"][block.get("id")] = (kind, command)
                if kind == "agents" and (block.get("input") or {}).get(
                        "run_in_background") is False:
                    st["blocking"].add(block.get("id"))
            elif btype == "tool_result":
                self._start(st, block)
            elif btype == "text" and from_user:
                # Only trust notifications the harness sent us: an agent
                # quoting one back (as in this very repo) must not count.
                self._finish(st, block.get("text") or "")

    @staticmethod
    def _start(st, block):
        tool_id = block.get("tool_use_id")
        st["blocking"].discard(tool_id)
        dispatch = st["dispatched"].pop(tool_id, None)
        if dispatch is None:
            return
        kind, command = dispatch
        body = block.get("content")
        body = body if isinstance(body, str) else json.dumps(body)
        m = TASK_ID_RE[kind].search(body)
        if not m:
            return
        task = m.group(1)
        st["active"][task] = kind
        if command:
            # A monitor or background command keeps its shell alive for as long
            # as it runs, so the command itself is the surest sign it is still
            # going — one a resume, a compaction or a quiet hour cannot erase.
            st["cmds"][task] = normalize_cmd(command)[:CMD_NEEDLE]
        out = OUTPUT_PATH_RE.search(body)
        if out:
            # Authoritative, and it survives a resume: after one, the live
            # session's task directory is not where older work was writing.
            st["outputs"][task] = Path(out.group(1))

    @staticmethod
    def _finish(st, text):
        if "<task-notification>" not in text:
            return
        for body in NOTIFICATION_RE.findall(text):
            task = NOTIFICATION_TASK_RE.search(body)
            if task and NOTIFICATION_DONE_RE.search(body):
                st["active"].pop(task.group(1), None)
                st["outputs"].pop(task.group(1), None)
                st["cmds"].pop(task.group(1), None)
                st["checked"].pop(task.group(1), None)


# ---------------------------------------------------------------------------
# SSE hub

class Hub:
    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)

    def broadcast(self, event, data):
        payload = (event, json.dumps(data))
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ---------------------------------------------------------------------------
# The monitor: ties detection to the store

class Monitor(threading.Thread):
    def __init__(self, store, hub):
        super().__init__(daemon=True)
        self.store = store
        self.hub = hub
        self.live = {}      # pid -> {session_id, thread_id, vterm, agent, cwd, missing}
        self.unclaimed = {} # pid -> {vterm, cwd}: agents with no registered thread
        self.poll_count = 0
        self.background = BackgroundWork()
        self.bg_counts = {} # thread id -> {agents, monitors, commands}, non-zero only

    # -- thread identity ----------------------------------------------------
    #
    # Threads are only ever created by explicit registration (`rt start`).
    # The monitor merely attaches live processes to registered threads, so an
    # agent session that was never registered is simply not a thread.

    def find_thread(self, vterm, cwd):
        if vterm:
            rows = self.store.query(
                "SELECT id FROM threads WHERE key = ?", ("vterm:%s" % vterm,))
            if rows:
                return rows[0]["id"]
        if cwd:
            rows = self.store.query(
                "SELECT id FROM threads WHERE key = ?", ("cwd:%s" % cwd,))
            if rows:
                return rows[0]["id"]
        return None

    # -- polling ------------------------------------------------------------

    def poll(self):
        now = int(time.time())
        procs = scan_agent_processes()
        all_procs = scan_all_processes()
        changed = False

        # New sessions
        for pid, p in procs.items():
            if pid in self.live:
                self.live[pid]["missing"] = 0
                if self.poll_count % CWD_REFRESH_EVERY == 0:
                    cwd = read_proc_cwd(pid)
                    if cwd and cwd != self.live[pid]["cwd"]:
                        self.live[pid]["cwd"] = cwd
                        self.store.execute(
                            "UPDATE threads SET cwd = ? WHERE id = ?",
                            (cwd, self.live[pid]["thread_id"]),
                        )
                        changed = True
                continue
            if pid in self.unclaimed:
                cached = self.unclaimed[pid]
                vterm, cwd = cached["vterm"], cached["cwd"]
            else:
                env = read_proc_env(pid)
                vterm = env.get("CLAUDE_VTERM_NAME")
                cwd = read_proc_cwd(pid) or env.get("PWD")
            tid = self.find_thread(vterm, cwd)
            if tid is None:
                # Not a registered research thread — remember it quietly so we
                # can claim it the moment a matching thread is registered.
                self.unclaimed[pid] = {"vterm": vterm, "cwd": cwd}
                continue
            self.unclaimed.pop(pid, None)
            started = read_proc_start(pid)
            sid = self.store.execute(
                "INSERT INTO sessions (thread_id, pid, agent, cwd, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tid, pid, p["agent"], cwd, started),
            )
            self.store.execute(
                "UPDATE threads SET agent = ? WHERE id = ?", (p["agent"], tid)
            )
            self.live[pid] = {
                "session_id": sid, "thread_id": tid, "vterm": vterm,
                "agent": p["agent"], "cwd": cwd, "missing": 0,
            }
            log("session start pid=%s agent=%s vterm=%r cwd=%r" % (pid, p["agent"], vterm, cwd))
            changed = True

        # Forget unclaimed agents whose processes have exited
        for pid in list(self.unclaimed):
            if pid not in procs:
                del self.unclaimed[pid]

        # Ended sessions (grace period so a brief ps hiccup doesn't close threads)
        for pid in list(self.live):
            if pid in procs:
                continue
            self.live[pid]["missing"] += 1
            if self.live[pid]["missing"] < CLOSED_GRACE_POLLS:
                continue
            info = self.live.pop(pid)
            self.store.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?", (now, info["session_id"])
            )
            self.store.record_status(info["thread_id"], "closed", now)
            self.store.set_unread(info["thread_id"], None)
            self.store.touch(info["thread_id"], now)
            log("session end pid=%s thread=%s" % (pid, info["thread_id"]))
            changed = True

        # Status per live thread
        open_threads = {}
        for pid, info in self.live.items():
            open_threads.setdefault(info["thread_id"], []).append(pid)

        # In-flight subagents / monitors / background commands
        self.background.refresh(set(self.live))
        bg = {}
        for tid, pids in open_threads.items():
            total = {k: 0 for k in BackgroundWork.KINDS}
            for pid in pids:
                for k, v in self.background.counts(pid).items():
                    total[k] += v
            if any(total.values()):
                bg[tid] = total
        if bg != self.bg_counts:
            self.bg_counts = bg
            changed = True
        for tid, pids in open_threads.items():
            status = None
            vterm = next((self.live[p]["vterm"] for p in pids if self.live[p]["vterm"]), None)
            if vterm:
                status, _ = read_state_file(vterm)
            if status in (None, "unknown"):
                cpu = max(subtree_cpu(p, all_procs) for p in pids)
                status = "working" if cpu > 20.0 else "idle"
            previous = self.store.latest_status(tid)
            if self.store.record_status(tid, status, now):
                changed = True
                # An agent that just stopped working left behind a reply the
                # user has not read yet; going back to work means they have.
                if status == "idle" and previous == "working":
                    self.store.set_unread(tid, now)
                elif status == "working":
                    self.store.set_unread(tid, None)
            if status == "working":
                self.store.touch(tid, now)

        if changed:
            self.hub.broadcast("state", snapshot(self.store, self))

    def run(self):
        while True:
            try:
                self.poll()
            except Exception as e:  # keep the monitor alive no matter what
                log("poll error: %r" % e)
            self.poll_count += 1
            time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Snapshot (what the UIs consume)

def snapshot(store, monitor):
    open_ids = {info["thread_id"] for info in monitor.live.values()}
    threads = store.query(
        """
        SELECT t.*,
          (SELECT COUNT(*) FROM notes n WHERE n.thread_id = t.id) AS note_count,
          (SELECT COUNT(*) FROM sessions s WHERE s.thread_id = t.id) AS session_count
        FROM threads t
        ORDER BY t.last_active_at DESC
        """
    )
    latest_events = {
        r["thread_id"]: r
        for r in store.query(
            """SELECT e.thread_id, e.status, e.ts FROM events e
               JOIN (SELECT thread_id, MAX(id) AS mid FROM events GROUP BY thread_id) x
               ON e.id = x.mid"""
        )
    }
    latest_notes = {
        r["thread_id"]: r
        for r in store.query(
            """SELECT n.thread_id, n.kind, n.text, n.path, n.ts, n.author FROM notes n
               JOIN (SELECT thread_id, MAX(id) AS mid FROM notes GROUP BY thread_id) x
               ON n.id = x.mid"""
        )
    }
    out = []
    for t in threads:
        is_open = t["id"] in open_ids
        ev = latest_events.get(t["id"])
        status = (ev or {}).get("status") if is_open else "closed"
        if is_open and status in (None, "closed"):
            status = "idle"
        out.append({
            **t,
            "open": is_open,
            "status": status,
            "unread": bool(is_open and status == "idle" and t["unread_at"]),
            "background": monitor.bg_counts.get(t["id"]) if is_open else None,
            "status_since": (ev or {}).get("ts"),
            "latest_note": latest_notes.get(t["id"]),
        })
    return {"threads": out, "now": int(time.time())}


def thread_detail(store, monitor, tid):
    rows = store.query("SELECT * FROM threads WHERE id = ?", (tid,))
    if not rows:
        return None
    t = rows[0]
    snap = {x["id"]: x for x in snapshot(store, monitor)["threads"]}
    t.update({k: snap[tid][k] for k in ("open", "status", "status_since", "unread",
                                        "background", "note_count", "session_count")})
    t["notes"] = store.query(
        "SELECT * FROM notes WHERE thread_id = ? ORDER BY ts DESC, id DESC LIMIT 500", (tid,)
    )
    t["sessions"] = store.query(
        "SELECT * FROM sessions WHERE thread_id = ? ORDER BY started_at DESC LIMIT 100", (tid,)
    )
    t["events"] = store.query(
        "SELECT ts, status FROM events WHERE thread_id = ? ORDER BY ts DESC, id DESC LIMIT 200", (tid,)
    )
    return t


# ---------------------------------------------------------------------------
# Thread resolution for agent-submitted notes

def resolve_target_thread(store, monitor, body):
    """Find the registered thread a request belongs to (never creates one)."""
    if body.get("thread_id"):
        rows = store.query("SELECT id FROM threads WHERE id = ?", (body["thread_id"],))
        return rows[0]["id"] if rows else None
    if body.get("vterm"):
        rows = store.query("SELECT id FROM threads WHERE key = ?", ("vterm:%s" % body["vterm"],))
        if rows:
            return rows[0]["id"]
    cwd = body.get("cwd")
    if cwd:
        # Prefer an open thread whose cwd matches, then the most recent match.
        open_ids = tuple({i["thread_id"] for i in monitor.live.values()}) or (-1,)
        qmarks = ",".join("?" * len(open_ids))
        rows = store.query(
            "SELECT id FROM threads WHERE cwd = ? AND id IN (%s)"
            " ORDER BY last_active_at DESC LIMIT 1" % qmarks,
            (cwd, *open_ids),
        )
        if not rows:
            rows = store.query(
                "SELECT id FROM threads WHERE cwd = ? ORDER BY last_active_at DESC LIMIT 1",
                (cwd,),
            )
        if rows:
            return rows[0]["id"]
    return None


NOT_REGISTERED = ("no registered thread matches this session; register one first"
                  " with: rt start <name> -o \"objective\"")


def register_thread(store, monitor, body):
    """Create or update a thread from an explicit `rt start` registration."""
    now = int(time.time())
    vterm, cwd = body.get("vterm"), body.get("cwd")
    if vterm:
        key = "vterm:%s" % vterm
    elif cwd:
        key = "cwd:%s" % cwd
    else:
        return None
    name = (body.get("name") or "").strip() or vterm or os.path.basename(cwd.rstrip("/"))
    agent = body.get("author") if body.get("author") in ("claude", "codex") else None
    tid, created = store.upsert_thread(key, name, agent, cwd, now)
    updates = {"name": name, "archived": 0}
    if body.get("objective"):
        updates["objective"] = body["objective"].strip()
    sets = ", ".join("%s = ?" % k for k in updates)
    store.execute("UPDATE threads SET %s WHERE id = ?" % sets, (*updates.values(), tid))
    monitor.unclaimed.clear()  # let the next poll claim already-running agents
    log("%s thread #%s %r via registration" % ("created" if created else "updated", tid, name))
    return tid


# ---------------------------------------------------------------------------
# HTTP

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf", ".woff2": "font/woff2", ".ico": "image/x-icon",
}


def make_handler(store, hub, monitor):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass  # quiet; we do our own logging

        # -- helpers --------------------------------------------------------

        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                return {}

        def serve_file(self, path):
            try:
                data = path.read_bytes()
            except OSError:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        # -- GET ------------------------------------------------------------

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path == "/api/state":
                return self.send_json(snapshot(store, monitor))
            if path == "/api/health":
                return self.send_json({"ok": True, "pid": os.getpid()})
            m = re.match(r"^/api/threads/(\d+)$", path)
            if m:
                detail = thread_detail(store, monitor, int(m.group(1)))
                if detail is None:
                    return self.send_json({"error": "no such thread"}, 404)
                return self.send_json(detail)
            if path == "/api/events":
                return self.serve_sse()
            if path.startswith("/plots/"):
                target = (PLOTS_DIR / path[len("/plots/"):]).resolve()
                if not str(target).startswith(str(PLOTS_DIR.resolve())):
                    return self.send_json({"error": "forbidden"}, 403)
                return self.serve_file(target)
            # Static web app
            rel = "index.html" if path == "/" else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
                target = WEB_DIR / "index.html"
            return self.serve_file(target)

        def serve_sse(self):
            q = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                snap = json.dumps(snapshot(store, monitor))
                self.wfile.write(("event: state\ndata: %s\n\n" % snap).encode())
                self.wfile.flush()
                while True:
                    try:
                        event, data = q.get(timeout=15)
                        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, data)).encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

        # -- POST -----------------------------------------------------------

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            body = self.read_body()
            now = int(time.time())

            if path == "/api/register":
                tid = register_thread(store, monitor, body)
                if tid is None:
                    return self.send_json({"error": "need vterm or cwd to register"}, 400)
                hub.broadcast("state", snapshot(store, monitor))
                name = store.query("SELECT name FROM threads WHERE id = ?", (tid,))[0]["name"]
                return self.send_json({"ok": True, "thread_id": tid, "thread": name})

            if path in ("/api/objective", "/api/status"):
                text = (body.get("text") or "").strip()
                if not text:
                    return self.send_json({"error": "empty text"}, 400)
                tid = resolve_target_thread(store, monitor, body)
                if tid is None:
                    return self.send_json({"error": NOT_REGISTERED}, 404)
                if path == "/api/objective":
                    store.execute("UPDATE threads SET objective = ? WHERE id = ?", (text, tid))
                else:
                    store.execute(
                        "UPDATE threads SET status_text = ?, status_updated_at = ? WHERE id = ?",
                        (text, now, tid))
                store.touch(tid, now)
                hub.broadcast("state", snapshot(store, monitor))
                name = store.query("SELECT name FROM threads WHERE id = ?", (tid,))[0]["name"]
                return self.send_json({"ok": True, "thread_id": tid, "thread": name})

            if path == "/api/notes":
                text = (body.get("text") or "").strip()
                if not text:
                    return self.send_json({"error": "empty note"}, 400)
                tid = resolve_target_thread(store, monitor, body)
                if tid is None:
                    return self.send_json({"error": NOT_REGISTERED}, 404)
                store.execute(
                    "INSERT INTO notes (thread_id, ts, author, kind, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (tid, now, body.get("author") or "unknown",
                     body.get("kind") or "note", text),
                )
                store.touch(tid, now)
                hub.broadcast("state", snapshot(store, monitor))
                name = store.query("SELECT name FROM threads WHERE id = ?", (tid,))[0]["name"]
                return self.send_json({"ok": True, "thread_id": tid, "thread": name})

            if path == "/api/plots":
                tid = resolve_target_thread(store, monitor, body)
                if tid is None:
                    return self.send_json({"error": NOT_REGISTERED}, 404)
                src = body.get("path")
                if not src:
                    return self.send_json({"error": "missing 'path'"}, 400)
                src = Path(src).expanduser()
                if not src.is_absolute() and body.get("cwd"):
                    src = Path(body["cwd"]) / src
                if not src.is_file():
                    return self.send_json({"error": "file not found: %s" % src}, 400)
                if src.suffix.lower() not in PLOT_EXTENSIONS:
                    return self.send_json(
                        {"error": "unsupported type %s" % src.suffix}, 400)
                dest_dir = PLOTS_DIR / str(tid)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / ("%d_%s" % (now, src.name))
                shutil.copyfile(src, dest)
                rel = "%s/%s" % (tid, dest.name)
                store.execute(
                    "INSERT INTO notes (thread_id, ts, author, kind, text, path)"
                    " VALUES (?, ?, ?, 'plot', ?, ?)",
                    (tid, now, body.get("author") or "unknown",
                     body.get("caption") or src.name, rel),
                )
                store.touch(tid, now)
                hub.broadcast("state", snapshot(store, monitor))
                return self.send_json({"ok": True, "thread_id": tid, "url": "/plots/" + rel})

            m = re.match(r"^/api/threads/(\d+)/(\w+)$", path)
            if m:
                tid, action = int(m.group(1)), m.group(2)
                if not store.query("SELECT id FROM threads WHERE id = ?", (tid,)):
                    return self.send_json({"error": "no such thread"}, 404)
                if action in ("archive", "unarchive"):
                    store.execute("UPDATE threads SET archived = ? WHERE id = ?",
                                  (1 if action == "archive" else 0, tid))
                elif action in ("pin", "unpin"):
                    store.execute("UPDATE threads SET pinned = ? WHERE id = ?",
                                  (1 if action == "pin" else 0, tid))
                elif action == "rename":
                    name = (body.get("name") or "").strip()
                    if not name:
                        return self.send_json({"error": "empty name"}, 400)
                    store.execute("UPDATE threads SET name = ? WHERE id = ?", (name, tid))
                elif action == "focus":
                    store.set_unread(tid, None)  # jumping to the vterm = reading it
                    self.focus_thread(tid)
                elif action == "mark_read":
                    store.set_unread(tid, None)
                elif action == "delete_note":
                    store.execute("DELETE FROM notes WHERE id = ? AND thread_id = ?",
                                  (body.get("note_id"), tid))
                else:
                    return self.send_json({"error": "unknown action"}, 400)
                hub.broadcast("state", snapshot(store, monitor))
                return self.send_json({"ok": True})

            return self.send_json({"error": "not found"}, 404)

        def focus_thread(self, tid):
            """Best-effort: raise Emacs and switch to the thread's vterm buffer."""
            t = store.query("SELECT key, name FROM threads WHERE id = ?", (tid,))[0]
            if not t["key"].startswith("vterm:"):
                return
            buf = "*vterm-%s*" % t["key"][len("vterm:"):]
            elisp = ('(progn (select-frame-set-input-focus (selected-frame))'
                     ' (switch-to-buffer "%s"))' % buf.replace("\\", "\\\\").replace('"', '\\"'))
            subprocess.Popen(["emacsclient", "-n", "-e", elisp],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.Popen(["osascript", "-e", 'tell application "Emacs" to activate'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return Handler


# ---------------------------------------------------------------------------

def main():
    if "--open" in sys.argv:
        subprocess.Popen(["open", "http://localhost:%d" % PORT])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    hub = Hub()
    monitor = Monitor(store, hub)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(store, hub, monitor))
    except OSError:
        log("port %d busy — another instance is probably running; exiting" % PORT)
        sys.exit(0)
    monitor.start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("research-threads server on http://localhost:%d (pid %d)" % (PORT, os.getpid()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
