#!/usr/bin/env python3
"""Primers at context start — Claude Code SessionStart and SubagentStart hook.

Hands every hygiene/primer-*.md to a context as it starts, so the
judgment rules are in place before anything is drafted rather than
arriving with the first watched command, after the draft.

It also writes the primer markers bash-policy.py reads, at a baseline
the per-command hook measures growth from. A fresh, cleared or compacted
context starts at 0; a resumed one starts at the transcript's size,
since the whole conversation is back in context ahead of the primer.

Fails open: any error exits 0 with no output.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EVENTS = ("SessionStart", "SubagentStart")
LEAD = "The user's standing preferences for commits, PRs, issues and code comments:"


def safe(value):
    return re.sub(r"[^A-Za-z0-9-]", "_", str(value)) if value else None


def main():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    if event not in EVENTS:
        return

    from bashpolicy import state

    session_id = safe(data.get("session_id")) or "nosession"
    agent_id = safe(data.get("agent_id")) if event == "SubagentStart" else None

    baseline = 0
    if event == "SessionStart" and data.get("source") == "resume":
        count, _ = state.transcript_context_tokens(data.get("transcript_path"))
        baseline = count or 0

    text = state.start_primers(session_id, agent_id, baseline)
    if text:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": f"{LEAD}\n\n{text}",
        }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
    sys.exit(0)
