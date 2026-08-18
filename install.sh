#!/bin/bash
# Research Threads installer. Idempotent — safe to re-run after git pull.
#
# What it does:
#   1. Symlinks bin/rt into ~/.local/bin
#   2. Symlinks the research-dashboard skill into ~/.claude/skills and
#      ~/.codex/skills, for whichever of the two agents is installed
#   3. Wires the session-status hooks (hooks/vterm-state.sh) into
#      ~/.claude/settings.json and ~/.codex/hooks.json, backing each up first
#   4. Installs a launchd agent so the server starts at login (macOS)
#   5. Optionally adds a load block for the Emacs dashboard to ~/.emacs.d/init.el
#
# Usage:
#   ./install.sh              # step 5 runs only if ~/.emacs.d/init.el exists
#   ./install.sh --emacs      # force the Emacs step
#   ./install.sh --no-emacs   # skip the Emacs step
#   ./install.sh --uninstall

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.research-threads.server"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
INIT_EL="${RESEARCH_THREADS_INIT_EL:-$HOME/.emacs.d/init.el}"
MARKER=";;; Research Threads dashboard (added by research_threads/install.sh)"
HOOK="$REPO/hooks/vterm-state.sh"

say() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
skip() { printf '  \033[2m· %s\033[0m\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

emacs_mode=auto
uninstall=no
for arg in "$@"; do
  case "$arg" in
    --emacs) emacs_mode=yes ;;
    --no-emacs) emacs_mode=no ;;
    --uninstall) uninstall=yes ;;
    *) echo "unknown option: $arg (see the header of install.sh)" >&2; exit 2 ;;
  esac
done

# Rewrites an agent's hook config: `wire` adds our entries, `unwire` strips
# them. Both are no-ops when the agent isn't installed or is already in the
# wanted state, and both back the file up before touching it.
hooks_py() {
  python3 - "$1" "$HOOK" <<'PY'
import json, os, shutil, sys, time

action, hook_cmd = sys.argv[1], sys.argv[2]
HOME = os.path.expanduser("~")

# Which agent event means which dashboard state. Codex has no "Notification"
# event (verified against the codex binary's HookEventsToml enum); its
# "needs you" signal is PermissionRequest, which Claude Code has as well.
COMMON = {
    "SessionStart": "idle", "UserPromptSubmit": "working", "PreToolUse": "working",
    "PermissionRequest": "needs-permission", "Stop": "idle", "SessionEnd": "end",
}
AGENTS = [
    {"name": "claude", "path": os.path.join(HOME, ".claude", "settings.json"),
     "events": dict(COMMON, **{"Notification": "needs-attention", "StopFailure": "idle"})},
    {"name": "codex", "path": os.path.join(HOME, ".codex", "hooks.json"),
     "events": COMMON},
]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(path, data, note):
    if os.path.exists(path):
        backup = "%s.bak-%d" % (path, int(time.time()))
        shutil.copyfile(path, backup)
        print("  \033[2m· backed up %s -> %s\033[0m" % (os.path.basename(path), backup))
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print("  \033[32m✓\033[0m %s" % note)


def is_ours(command):
    """True for any wiring of this hook, including an older copy elsewhere."""
    return "vterm-state.sh" in command


def wire(agent):
    data = load(agent["path"])
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, state in agent["events"].items():
        entries = hooks.setdefault(event, [])
        if any(is_ours(h.get("command", "")) for e in entries for h in e.get("hooks", [])):
            continue
        entry = {"type": "command", "command": "%s %s" % (hook_cmd, state), "async": True}
        if entries:
            entries[0].setdefault("hooks", []).append(entry)
        else:
            entries.append({"matcher": "*", "hooks": [entry]})
        changed = True
    if not changed:
        print("  \033[2m· %s status hooks already wired\033[0m" % agent["name"])
        return
    save(agent["path"], data, "%s status hooks wired in %s"
         % (agent["name"], agent["path"].replace(HOME, "~")))


def unwire(agent):
    data = load(agent["path"])
    hooks = data.get("hooks") or {}
    changed = False
    for event, entries in list(hooks.items()):
        for entry in list(entries):
            kept = [h for h in entry.get("hooks", []) if not is_ours(h.get("command", ""))]
            if len(kept) != len(entry.get("hooks", [])):
                changed = True
                entry["hooks"] = kept
            if not entry.get("hooks"):
                entries.remove(entry)
        if not entries:
            del hooks[event]
    if not changed:
        return
    save(agent["path"], data, "%s status hooks removed from %s"
         % (agent["name"], agent["path"].replace(HOME, "~")))


for agent in AGENTS:
    if not os.path.isdir(os.path.dirname(agent["path"])):
        continue  # agent not installed for this user
    (wire if action == "wire" else unwire)(agent)
PY
}

if [ "$uninstall" = yes ]; then
  launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
  rm -f "$PLIST_DEST" "$HOME/.local/bin/rt"
  rm -f "$HOME/.claude/skills/research-dashboard" "$HOME/.codex/skills/research-dashboard"
  hooks_py unwire
  if grep -qF "$MARKER" "$INIT_EL" 2>/dev/null; then
    perl -0pi -e "s/\n?\Q$MARKER\E.*?;;; End Research Threads\n?//s" "$INIT_EL"
    say "Emacs block removed from $INIT_EL"
  fi
  echo "Uninstalled. Data kept in ~/.research-threads (delete manually if desired)."
  exit 0
fi

echo "Installing Research Threads from $REPO"

# 1. rt CLI ------------------------------------------------------------------
mkdir -p "$HOME/.local/bin"
ln -sfn "$REPO/bin/rt" "$HOME/.local/bin/rt"
say "rt CLI -> ~/.local/bin/rt"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "~/.local/bin is not on your PATH — add it so agents can run rt" ;;
esac

# 2. Skills ------------------------------------------------------------------
installed_agent=no
for agent_home in "$HOME/.claude" "$HOME/.codex"; do
  [ -d "$agent_home" ] || continue
  installed_agent=yes
  mkdir -p "$agent_home/skills"
  ln -sfn "$REPO/skills/research-dashboard" "$agent_home/skills/research-dashboard"
  say "skill -> ${agent_home/#$HOME/\~}/skills/research-dashboard"
done
[ "$installed_agent" = yes ] || warn "neither ~/.claude nor ~/.codex found — skipped skill and hook setup"

# 3. Status hooks ------------------------------------------------------------
chmod +x "$HOOK"
hooks_py wire

# 4. launchd agent -----------------------------------------------------------
mkdir -p "$HOME/.research-threads"
if [ "$(uname -s)" = Darwin ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
    "$REPO/launchd/$PLIST_LABEL.plist" > "$PLIST_DEST"
  launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
  say "launchd agent installed (server starts at login)"
else
  warn "not macOS — no launch-at-login agent; run 'rt serve' to start the server"
fi

# 5. Emacs (optional) --------------------------------------------------------
if [ "$emacs_mode" = no ]; then
  skip "Emacs dashboard skipped (--no-emacs)"
elif [ "$emacs_mode" = auto ] && [ ! -f "$INIT_EL" ]; then
  skip "no $INIT_EL — Emacs dashboard skipped (re-run with --emacs to install)"
elif grep -qF "$MARKER" "$INIT_EL" 2>/dev/null; then
  skip "init.el already configured"
else
  mkdir -p "$(dirname "$INIT_EL")"
  cat >> "$INIT_EL" <<EOF

$MARKER
(load "$REPO/emacs/research-threads.el" t)
(global-set-key (kbd "C-c r") #'research-threads)
;;; End Research Threads
EOF
  say "Emacs dashboard added to $INIT_EL  (C-c r, or M-x research-threads)"
fi

echo
echo "Done. Dashboard: http://localhost:7878   CLI: rt help"
