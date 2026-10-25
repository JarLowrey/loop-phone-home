#!/usr/bin/env bash
# Hands one task from loop/TASKS.md to the local space-qwen model, run through Claude Code headless.
# Usage: tools/qwen_task.sh <task-id> [feedback-file]
# The feedback file holds the test failures from the last attempt, so qwen can fix them.
# Phone messages: the first 2 progress messages come every EARLY_PROGRESS_INTERVAL seconds (default 240, 4 minutes), then
# one every PROGRESS_INTERVAL seconds (default 1200, 20 minutes). Also start, finish and
# failure messages. Every STUCK_CHECK_INTERVAL seconds (default 210, 3.5 minutes) it checks whether the log has grown.
# If not, it sends a "may be stuck" alert at once, once per stall. They use the topic in
# ~/.claude/hooks/ntfy-topic.txt. If that file is missing, nothing is sent.
# Set QWEN_NOTIFY_DRY=1 to print the messages instead of sending them.
# Set CLAUDE_EXE if Claude Code is not on PATH.
set -u
TASK_ID="${1:?usage: qwen_task.sh <task-id> [feedback-file]}"
FEEDBACK="${2:-}"
CLAUDE="${CLAUDE_EXE:-claude}"
PROGRESS_INTERVAL="${PROGRESS_INTERVAL:-1200}"
EARLY_PROGRESS_INTERVAL="${EARLY_PROGRESS_INTERVAL:-240}"
EARLY_PROGRESS_REPORTS=2
STUCK_CHECK_INTERVAL="${STUCK_CHECK_INTERVAL:-210}"
cd "$(dirname "$0")/../.." || exit 1

TOPIC=""
if [ -f "$HOME/.claude/hooks/ntfy-topic.txt" ]; then
	TOPIC=$(tr -d '\r\n ' < "$HOME/.claude/hooks/ntfy-topic.txt")
fi

# $1 title, $2 body, $3 priority
notify() {
	if [ "${QWEN_NOTIFY_DRY:-0}" = "1" ]; then
		echo "[notify] $1 | $2"
		return 0
	fi
	[ -z "$TOPIC" ] && return 0
	curl -s -m 20 -o /dev/null -H "Title: $1" -H "Priority: ${3:-default}" -d "${2:0:500}" "https://ntfy.sh/$TOPIC" || true
}

spec=$(awk -v id="$TASK_ID" '
	$0 ~ ("^### \\[.\\] " id " ") { p = 1 }
	p && /^### / && $0 !~ ("^### \\[.\\] " id " ") { exit }
	p && /^---$/ { exit }
	p { print }
' loop/TASKS.md)
if [ -z "$spec" ]; then
	echo "no task $TASK_ID in loop/TASKS.md"
	exit 1
fi

prompt="Do this task from loop/TASKS.md. Work in the space-game folder and change only what the task asks for.
Follow CLAUDE.md: GDScript, tabs, class_name, one ## doc line per script and @export field, seeded randomness only,
tests in tests/test_<thing>.gd. Never delete, skip or weaken a test. Never edit GoBlaster2.
When the code is written, run ./space-game/tools/run_tests.sh and fix any FAIL lines.
Finish with the test summary line and the files you changed.

$spec"
if [ -n "$FEEDBACK" ] && [ -f "$FEEDBACK" ]; then
	prompt="$prompt

The last attempt failed. Its test output:
$(tail -n 60 "$FEEDBACK")"
fi

mkdir -p loop/logs
log="loop/logs/$TASK_ID.log"
start=$(date +%s)

# Runs in the background, checking every 5 seconds which timer is due.
# Stuck check (every STUCK_CHECK_INTERVAL): if the log has not grown since the last check, send one "may be stuck"
# alert. It is not repeated until the log grows again.
# Progress report: one status message with the time, tool calls, last file and diff size. The first
# EARLY_PROGRESS_REPORTS come every EARLY_PROGRESS_INTERVAL, then every PROGRESS_INTERVAL.
report_progress() {
	local last_report=$start last_check=$start last_size=0 alerted=0 reports=0 wait_for now size mins calls file diff msg
	while sleep 5; do
		now=$(date +%s)
		if [ $(( now - last_check )) -ge "$STUCK_CHECK_INTERVAL" ]; then
			last_check=$now
			size=$(wc -c < "$log" 2>/dev/null || echo 0)
			if [ "$size" -eq "$last_size" ]; then
				if [ "$alerted" -eq 0 ]; then
					notify "Qwen may be stuck" "$TASK_ID: no new output for $STUCK_CHECK_INTERVAL s. Check loop/logs/$TASK_ID.log" high
					alerted=1
				fi
			else
				alerted=0
			fi
			last_size=$size
		fi
		wait_for=$PROGRESS_INTERVAL
		[ "$reports" -lt "$EARLY_PROGRESS_REPORTS" ] && wait_for=$EARLY_PROGRESS_INTERVAL
		if [ $(( now - last_report )) -ge "$wait_for" ]; then
			last_report=$now
			reports=$(( reports + 1 ))
			mins=$(( (now - start) / 60 ))
			calls=$(grep -o '"type":"tool_use"' "$log" 2>/dev/null | wc -l)
			file=$(grep -o '"file_path":"[^"]*"' "$log" 2>/dev/null | tail -n 1 | sed 's/.*[\\/]//; s/"$//')
			diff=$(git diff --stat -- space-game 2>/dev/null | tail -n 1)
			msg="$TASK_ID: ${mins} min elapsed, $calls tool calls, last file ${file:-none}. ${diff:-no changes yet}"
			notify "Qwen progress" "$msg" default
		fi
	done
}

notify "Qwen run started" "$TASK_ID started"
report_progress >> "loop/logs/$TASK_ID.notify.log" 2>&1 &
reporter=$!
trap 'kill "$reporter" 2>/dev/null' EXIT

# The session is trimmed to fit qwen's 131K window (see loop/Modelfile). Claude Code's default system prompt and tool
# list take about 21K tokens. Trimmed, they take about 4.6K. Each tool result adds to the prompt, and past the window
# Ollama cuts the prompt without an error, so keep the task small.
# The env lines must run straight into the claude call. A comment inside the backslash chain ends the command, and
# then claude gets no Ollama settings and calls Anthropic's API instead.
ANTHROPIC_BASE_URL=http://localhost:11434 \
ANTHROPIC_AUTH_TOKEN=ollama \
ANTHROPIC_API_KEY= \
ANTHROPIC_DEFAULT_HAIKU_MODEL=space-qwen \
ANTHROPIC_DEFAULT_SONNET_MODEL=space-qwen \
ANTHROPIC_DEFAULT_OPUS_MODEL=space-qwen \
"$CLAUDE" -p "$prompt" \
	--model space-qwen \
	--permission-mode acceptEdits \
	--tools Bash,Read,Edit,Write,Glob,Grep \
	--disable-slash-commands \
	--strict-mcp-config \
	--system-prompt "You are a coding agent for a Godot 4.6 project. Read only the files the task names, and search with Grep before opening anything large." \
	--allowedTools "Bash(./space-game/tools/run_tests.sh)" "Bash(ls:*)" "Bash(grep:*)" "Bash(cat:*)" \
	--output-format stream-json --verbose \
	2>&1 | tee "$log"
status=${PIPESTATUS[0]}

kill "$reporter" 2>/dev/null
if [ "$status" -eq 0 ]; then
	notify "Qwen run finished" "$TASK_ID finished with exit 0 after $(( ($(date +%s) - start) / 60 )) min"
else
	notify "Qwen run failed" "$TASK_ID exited with $status after $(( ($(date +%s) - start) / 60 )) min. See $log" high
fi
exit "$status"
