#!/usr/bin/env python3
"""Centralized Bash policy — Claude Code PreToolUse hook.

One registration (matcher "Bash", no `if`) covering everything the two
previous hooks did:

  * commit-message / PR / issue hygiene  (was hooks/hygiene-dispatch.py)
  * git history safety and commit signing (was ~/.claude/hooks/git-history-guard.py)

Why one hook rather than two: both parsed the same command string, and
the safety-critical one carried the weaker parser — `shlex.split`, which
cannot see into heredocs and returned (allowing the command) on anything
it failed to parse. Sharing `bashpolicy.shell` gives the history rules
the quote/heredoc-aware splitter, and matching on a normalized command
path means `git -C /elsewhere commit` is now policed too, which the
hygiene dispatcher's raw leading-word match missed.

Decisions from all rules are folded with Claude Code's own precedence
for multiple PreToolUse hooks — deny > defer > ask > allow, and
order-independent — so collapsing two hooks into one cannot change which
decision wins. `allow` is opt-in per rule, so folding the history rules
in does not start auto-approving `git push`.

Failure policy:
  * ADVISORY rule raises  -> skipped; a style gate never blocks work.
  * CRITICAL rule raises  -> deny; the old guard failed open on its own
                             bugs, silently dropping push protection.
  * Command unparseable   -> deny only if it textually looks like a
                             history-affecting git command, else silent.
  * Anything else (bad stdin, import error) -> exit 0, no output.

Layout:
  Primers: <this repo>/hygiene/primer-<cat>.md (resolved relative to the
           package, so the repo stays self-contained)
  State:   ~/.claude/hooks/.state/         ($BASH_POLICY_HOME to override)
  Config:  ~/.claude/hooks/hygiene-config.json
           {"signoff_cwd_substrings": ["/path/fragment", ...]}
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Budget for all git inspection in one hook invocation. The signature
# check is a single `git log` now, so this is a backstop rather than the
# load-bearing bound the old per-commit loop needed.
GIT_BUDGET_S = 20.0

# Crude textual net for the parse-failure path: if the parser cannot
# handle the command, these are the ones we refuse rather than wave
# through unchecked.
CRITICAL_FALLBACK_RE = re.compile(r"\bgit\b[^\n;|&]*\b(?:push|commit|rebase)\b")


class Context:
    """Per-invocation facts the rules need."""

    __slots__ = ("session_id", "cwd", "transcript_path", "config", "_deadline")

    def __init__(self, payload, config):
        self.session_id = re.sub(
            r"[^A-Za-z0-9-]", "_", str(payload.get("session_id") or "nosession")
        )
        self.cwd = payload.get("cwd") or os.getcwd()
        self.transcript_path = payload.get("transcript_path")
        self.config = config
        self._deadline = time.monotonic() + GIT_BUDGET_S

    def remaining_budget(self):
        return self._deadline - time.monotonic()


def emit(decision=None, reason=None, context=None):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
    h = out["hookSpecificOutput"]
    if decision:
        h["permissionDecision"] = decision
        if reason:
            h["permissionDecisionReason"] = reason
    if context:
        h["additionalContext"] = context
    print(json.dumps(out))


def main():
    data = json.load(sys.stdin)
    if data.get("hook_event_name") != "PreToolUse":
        return
    if data.get("tool_name") != "Bash":
        return
    cmd = (data.get("tool_input") or {}).get("command")
    if not cmd or not isinstance(cmd, str):
        return

    from bashpolicy import githist, hygiene, shell, state  # noqa: F401
    from bashpolicy.policy import evaluate

    ctx = Context(data, state.load_config())

    try:
        invocations = shell.parse(cmd)
    except shell.ParseError as exc:
        if CRITICAL_FALLBACK_RE.search(cmd):
            emit(decision="deny", reason=(
                "This command could not be parsed well enough to check it "
                f"against the git history-safety rules ({exc}). Refusing rather "
                "than allowing an unchecked git push/commit/rebase — split it "
                "into simpler commands."))
        return

    verdict = evaluate(invocations, ctx)
    if verdict.decision:
        emit(decision=verdict.decision, reason=verdict.reason,
             context=verdict.context)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail open on anything the rule tiers did not already handle:
        # never break every Bash call on a dispatcher bug.
        sys.exit(0)
    sys.exit(0)
