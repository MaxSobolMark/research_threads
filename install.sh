#!/bin/bash
# Research Threads installer. Idempotent — safe to re-run after git pull.
#
# What it does:
#   1. Symlinks bin/rt into ~/.local/bin
#   2. Symlinks the research-dashboard skill into ~/.claude/skills and
#      ~/.codex/skills (so both agents know how to post to the dashboard)
#   3. Wires Codex status hooks in ~/.codex/hooks.json (backs it up first),
#      mirroring the Claude Code hooks that already write ~/.claude/state
#   4. Installs a launchd agent so the server starts at login
#   5. Adds a guarded load block for the Emacs dashboard to ~/.emacs.d/init.el
#
# Undo: ./install.sh --uninstall

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.research-threads.server"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
INIT_EL="$HOME/.emacs.d/init.el"
MARKER=";;; Research Threads dashboard (added by research_threads/install.sh)"

say() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
skip() { printf '  \033[2m· %s\033[0m\n' "$1"; }

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
  rm -f "$PLIST_DEST" "$HOME/.local/bin/rt"
  rm -f "$HOME/.claude/skills/research-dashboard" "$HOME/.codex/skills/research-dashboard"
  if grep -qF "$MARKER" "$INIT_EL" 2>/dev/null; then
    perl -0pi -e "s/\n?\Q$MARKER\E.*?;;; End Research Threads\n?//s" "$INIT_EL"
  fi
  echo "Uninstalled. Data kept in ~/.research-threads (delete manually if desired)."
  echo "Codex hooks.json was NOT reverted; backups are at ~/.codex/hooks.json.bak-*"
  exit 0
fi

echo "Installing Research Threads from $REPO"

# 1. rt CLI ------------------------------------------------------------------
mkdir -p "$HOME/.local/bin"
ln -sfn "$REPO/bin/rt" "$HOME/.local/bin/rt"
say "rt CLI -> ~/.local/bin/rt"

# 2. Skills ------------------------------------------------------------------
for dir in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
  mkdir -p "$dir"
  ln -sfn "$REPO/skills/research-dashboard" "$dir/research-dashboard"
  say "skill -> $dir/research-dashboard"
done

# 3. Codex status hooks ------------------------------------------------------
python3 - "$HOME/.codex/hooks.json" <<'PY'
import json, sys, time, os, shutil
path = sys.argv[1]
hook_cmd = os.path.expanduser("~/.codex/hooks/vterm-state.sh")
if not os.path.exists(hook_cmd):
    shutil.copyfile(os.path.expanduser("~/.claude/hooks/vterm-state.sh"), hook_cmd)
    os.chmod(hook_cmd, 0o755)
try:
    with open(path) as f:
        data = json.load(f)
except (OSError, ValueError):
    data = {}
hooks = data.setdefault("hooks", {})
# Events verified against the codex binary's HookEventsToml enum — codex has
# no "Notification" event; PermissionRequest is its "needs you" signal.
wanted = {
    "SessionStart": "idle", "UserPromptSubmit": "working", "PreToolUse": "working",
    "PermissionRequest": "needs-permission", "Stop": "idle", "SessionEnd": "end",
}
changed = False
for event, state in wanted.items():
    cmd = "%s %s" % (hook_cmd, state)
    entries = hooks.setdefault(event, [])
    existing = [h.get("command", "") for e in entries for h in e.get("hooks", [])]
    if any(hook_cmd in c for c in existing):
        continue
    if entries:
        entries[0].setdefault("hooks", []).append(
            {"type": "command", "command": cmd, "async": True})
    else:
        entries.append({"matcher": "*",
                        "hooks": [{"type": "command", "command": cmd, "async": True}]})
    changed = True
if changed:
    backup = "%s.bak-%d" % (path, int(time.time()))
    if os.path.exists(path):
        shutil.copyfile(path, backup)
        print("  · backed up hooks.json -> %s" % backup)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print("  \033[32m✓\033[0m codex status hooks wired in ~/.codex/hooks.json")
else:
    print("  · codex hooks already wired")
PY

# 4. launchd agent -----------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.research-threads"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/launchd/$PLIST_LABEL.plist" > "$PLIST_DEST"
launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
say "launchd agent installed (server starts at login)"

# 5. Emacs -------------------------------------------------------------------
if [ -f "$INIT_EL" ] && ! grep -qF "$MARKER" "$INIT_EL"; then
  cat >> "$INIT_EL" <<EOF

$MARKER
(load "$REPO/emacs/research-threads.el" t)
(global-set-key (kbd "<f12>") #'research-threads)
(global-set-key (kbd "C-c r") #'research-threads)
;;; End Research Threads
EOF
  say "Emacs dashboard added to init.el  (F12, or M-x research-threads)"
else
  skip "init.el already configured"
fi

echo
echo "Done. Dashboard: http://localhost:7878   Emacs: C-c r   CLI: rt help"
