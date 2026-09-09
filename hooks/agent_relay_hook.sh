#!/bin/sh

# Portable Codex Hook wrapper for AgentRelay.
# The wrapper never calls an online Provider. It only forwards Tracker's
# PostToolUse additionalContext JSON to Codex.

HOME_DIR=${HOME:-$(pwd)}
CODEX_HOME=${CODEX_HOME:-"$HOME_DIR/.codex"}
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" 2>/dev/null && pwd -P)
TRACKER=${AGENT_RELAY_TRACKER_PATH:-"$SCRIPT_DIR/agent_relay_tracker.py"}
ORCA_HOOK=${ORCA_HOOK_PATH:-"$HOME_DIR/.orca/agent-hooks/codex-hook.sh"}

LOG_DIR="$CODEX_HOME/agent_relay_tracker/logs"
if ! mkdir -p "$LOG_DIR" 2>/dev/null; then
    LOG_DIR="${TMPDIR:-/tmp}/agent_relay"
    mkdir -p "$LOG_DIR" 2>/dev/null || :
fi

ERROR_LOG="$LOG_DIR/error.log"
DEBUG_LOG="$CODEX_HOME/agent_relay_tracker/hook_debug.log"

log_error() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$ERROR_LOG" 2>/dev/null || :
}

log_debug() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$DEBUG_LOG" 2>/dev/null || :
}

PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
    PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
elif [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v "$PYTHON" 2>/dev/null || true)
fi

PAYLOAD=$(command -p cat 2>>"$ERROR_LOG")
if [ -z "$PAYLOAD" ]; then
    log_debug "event=unknown stdin_empty=yes"
    exit 0
fi

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    log_error "Python interpreter not found; set PYTHON or install python3"
    exit 0
fi

EVENT_TYPE=$(
    printf '%s' "$PAYLOAD" |
        "$PYTHON" -c 'import json, sys; data=json.load(sys.stdin); print(data.get("event") or data.get("event_name") or data.get("eventName") or data.get("hook_event_name") or data.get("hookEventName") or "unknown")' \
        2>>"$ERROR_LOG"
)
EVENT_STATUS=$?
if [ "$EVENT_STATUS" -ne 0 ]; then
    EVENT_TYPE="unknown"
    log_error "Unable to parse Hook event name"
fi

log_debug "event=$EVENT_TYPE CODEX_SESSION_ID=${CODEX_SESSION_ID:-} preparing_tracker=yes"

TRACKER_OUTPUT=$(
    printf '%s' "$PAYLOAD" |
        "$PYTHON" "$TRACKER" 2>>"$ERROR_LOG"
)
TRACKER_EXIT_CODE=$?

if [ "$TRACKER_EXIT_CODE" -ne 0 ]; then
    log_error "Tracker exited with code $TRACKER_EXIT_CODE"
fi

# Preserve the existing Orca integration when it is installed, but never
# make it a required dependency and never let its output reach Codex.
if [ -x "$ORCA_HOOK" ]; then
    printf '%s' "$PAYLOAD" |
        /bin/sh "$ORCA_HOOK" >/dev/null 2>>"$ERROR_LOG" ||
        log_error "Orca Hook failed"
fi

# Only a complete PostToolUse hookSpecificOutput object is forwarded.
# Stop and all other events intentionally produce empty stdout.
if [ -n "$TRACKER_OUTPUT" ] &&
    printf '%s' "$TRACKER_OUTPUT" |
        "$PYTHON" -c 'import json, sys; data=json.load(sys.stdin); output=data.get("hookSpecificOutput"); assert isinstance(output, dict) and output.get("hookEventName") == "PostToolUse" and isinstance(output.get("additionalContext"), str)' \
        >/dev/null 2>>"$ERROR_LOG"; then
    printf '%s\n' "$TRACKER_OUTPUT"
elif [ -n "$TRACKER_OUTPUT" ]; then
    log_error "Tracker output was not valid PostToolUse hookSpecificOutput JSON"
fi

exit 0
