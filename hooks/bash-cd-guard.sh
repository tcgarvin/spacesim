#!/usr/bin/env bash
# PreToolUse:Bash guard.
#
# A leading `cd <project root> &&` (or `;`) is a no-op because the Bash tool
# already starts there, so it is rewritten away instead of denied. Every other
# `cd` is denied with a message that names the flag to use instead.
#
# Not `set -e` on purpose: a crash here must not block a Bash call. On any
# internal failure the script prints nothing and exits 0, which lets the
# command through unchanged.
set -uo pipefail

command -v jq >/dev/null 2>&1 || exit 0

payload=$(cat) || exit 0
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0

CD_RE='(^|[;&|(`]|&&|\|\|)[[:space:]]*cd([[:space:]]|$)'
printf '%s' "$cmd" | grep -qE "$CD_RE" || exit 0

root=${CLAUDE_PROJECT_DIR:-/home/timg/code/spacesim2}
# `%` is the sed delimiter: the path contains `/` and the alternation contains `|`.
stripped=$(printf '%s' "$cmd" |
  sed -E "s%^[[:space:]]*cd[[:space:]]+\"?${root}/?\"?[[:space:]]*(&&|;)[[:space:]]*%%") || exit 0

# Rewrite only when the strip removed the only cd in the command.
if [ "$stripped" != "$cmd" ] && ! printf '%s' "$stripped" | grep -qE "$CD_RE"; then
  jq -cn --argjson p "$payload" --arg new "$stripped" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      updatedInput: ($p.tool_input + {command: $new}),
      additionalContext: "Dropped a leading `cd <project root>`: Bash already starts there. Use absolute paths from now on."
    }
  }' 2>/dev/null
  exit 0
fi

reason='cd is blocked in this repo (in a compound command it also forces a permission
prompt). Bash already runs in the project root. Use absolute paths, or the
tool'"'"'s own directory flag:
  git -C DIR ...
  uv run --project DIR ...        (uses DIR'"'"'s venv)
  uv run --directory DIR ...      (runs from DIR, current venv)
  make -C DIR / pytest DIR/tests / rg PATTERN DIR
A leading `cd <project root> &&` is rewritten away automatically; anything else
must be expressed without cd.'

jq -cn --arg r "$reason" '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}' 2>/dev/null
exit 0
