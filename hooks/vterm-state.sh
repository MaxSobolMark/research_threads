#!/bin/bash
# Writes agent session state for an Emacs vterm, for the Research Threads
# dashboard. Invoked by hooks in ~/.claude/settings.json and ~/.codex/hooks.json
# (wired by install.sh). The state file is keyed on $CLAUDE_VTERM_NAME, exported
# by Emacs when launching the vterm; outside a named vterm this is a no-op, so
# the hook is harmless in ordinary terminal sessions.
#
# Usage: vterm-state.sh STATE
#   STATE: working | idle | needs-permission | needs-attention | end

set -u

state="${1:-unknown}"
payload="$(cat 2>/dev/null || true)"

# Claude's Notification hook also fires a periodic "Claude is waiting for
# your input" reminder when the session merely sits at the prompt. That is
# "ready", not "needs attention" — downgrade it so idle sessions don't flare.
if [ "$state" = "needs-attention" ] && printf '%s' "$payload" | grep -qi "waiting for your input"; then
  state="idle"
fi

[ -z "${CLAUDE_VTERM_NAME:-}" ] && exit 0

dir="$HOME/.claude/state"
mkdir -p "$dir"
file="$dir/${CLAUDE_VTERM_NAME}.json"

if [ "$state" = "end" ]; then
  rm -f "$file"
  exit 0
fi

ts=$(date +%s)
printf '{"state":"%s","ts":%d,"pid":%d}\n' "$state" "$ts" "$$" >"$file"
