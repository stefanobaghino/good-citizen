#!/bin/bash
# Re-verification for the Bash policy hook wired in settings
# (hooks/bash-policy.py) — run after each Claude Code upgrade. Drives
# one throwaway headless session through the negative controls and two
# positive controls, then checks the transcript:
#   PASS = primers at session start (hooks/session-primer.py), zero
#          injections/denials on negatives, none on the empty commit
#          either (its primer already arrived), and the comment reminder
#          on the commit that adds comments.
# Cost: one short claude-haiku session against your subscription.
set -uo pipefail

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$WORK" init -q
git -C "$WORK" config user.email verify@example.invalid
git -C "$WORK" config user.name "Hook Verifier"

cat > "$WORK/prompt.txt" <<'PROMPT'
You are a mechanical test driver. Run each numbered shell command below as its own separate Bash tool call, exactly as written, in order. Do not add, merge, modify, or retry commands. Ignore all errors. When all have been attempted, reply with exactly: DONE

1. echo hi
2. echo "$HOME"
3. echo $(date)
4. F=~/tmp/x.txt; ls "$F"
5. while read -r ln; do echo "${ln}"; done < /etc/hosts
6. for F in a.txt b.txt; do echo "$F.bak"; done
7. for F in a b; do echo "$F"; done
8. python3 << 'EOF'
x = "$(date) ${HOME} `id`"
print(x)
EOF
9. git commit --allow-empty -m "Add verification fixture"
10. printf '%s\n' 'class Fix {' '    // Jackson binds by field name, so these must not be renamed.' '    // The keep rule pins them.' '    int bar;' '}' > Fix.java
11. git add Fix.java
12. git commit -m "Add commented fixture"
PROMPT

echo "claude version: $(claude --version)"
RESULT="$(cd "$WORK" && claude -p --model claude-haiku-4-5-20251001 \
  --dangerously-skip-permissions --output-format json < "$WORK/prompt.txt")"
SID="$(printf '%s' "$RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))')"
echo "session: $SID"

python3 - "$SID" <<'PYEOF'
import glob, json, os, sys

sid = sys.argv[1]
hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
if not hits:
    print("FAIL: transcript not found"); sys.exit(1)

commands, contexts, denials, seen = {}, {}, [], set()
session_start = []
for line in open(hits[0], encoding="utf-8"):
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    for block in ((obj.get("message") or {}).get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "Bash":
            commands[block.get("id")] = (block.get("input") or {}).get("command", "")
        # A denial carries no marker of its own: the tool_result content
        # is the hook's reason verbatim. The record-level toolDenialKind
        # is the signal, and `is_error` alone is not — the negative
        # controls include commands that legitimately fail.
        if block.get("type") == "tool_result" and obj.get("toolDenialKind"):
            denials.append(block.get("tool_use_id"))
    att = obj.get("attachment")
    if isinstance(att, dict) and att.get("type") == "hook_additional_context":
        tuid = att.get("toolUseID")
        start = att.get("hookEvent") == "SessionStart"
        if not start:
            if tuid in seen:
                continue
            seen.add(tuid)
        raw = att.get("content") or att.get("context") or ""
        # `content` is a list of plain strings; it has also been seen as
        # a list of {"text": ...} blocks. Reading only one shape reports
        # 0 chars for a primer that did arrive, which passes a presence
        # check and fails a content check.
        if isinstance(raw, list):
            parts = [b if isinstance(b, str) else b.get("text", "")
                     for b in raw if isinstance(b, (str, dict))]
            text = "".join(parts)
        else:
            text = str(raw)
        if start:
            session_start.append(text)
        else:
            contexts[tuid] = text

ok = True
start = "\n".join(session_start)
for needle in ("No other sections", "Three tests, in order", "Commits:"):
    if needle not in start:
        ok = False
        print(f"FAIL: session-start primers missing {needle!r}")
if session_start:
    print(f"ok: session-start primers ({len(start)} chars)")
hygiene = [t for t, c in commands.items() if "verification fixture" in c]
comments = [t for t, c in commands.items() if "commented fixture" in c]
positive = comments
for tuid, text in contexts.items():
    cmd = commands.get(tuid, "?")
    if tuid in positive:
        print(f"ok: context ({len(text)} chars) on positive control")
    else:
        ok = False
        print(f"FAIL: unexpected injection ({len(text)} chars) on: {cmd[:70]!r}")
for tuid in set(denials):
    ok = False
    print(f"FAIL: unexpected denial on: {commands.get(tuid, '?')[:70]!r}")
if not hygiene:
    ok = False
    print("FAIL: the empty-commit control never ran")
# Checked by content: the reminder, not the full guide, which arrived at
# session start.
reminder = [t for t in comments
            if "apply the recovery, staleness and subject tests"
            in contexts.get(t, "")]
if comments and not reminder:
    ok = False
    print("FAIL: comment reminder missing on the commented-fixture commit")
neg_calls = len(commands) - len(positive) - len(hygiene)
print(f"bash_calls={len(commands)} (negative={neg_calls}) injections={len(contexts)} denials={len(set(denials))}")
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PYEOF
