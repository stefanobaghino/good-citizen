#!/usr/bin/env python3
"""Unit tests for hooks/bash-policy.py — no dependencies, no live session.

Run: python3 hooks/test-bash-policy.py

Complements hooks/verify-bash-policy.sh, which drives a real headless
Claude Code session (and costs one) to confirm the hook is wired up and
stays quiet on tricky quoting. This suite exercises the decisions
directly: it feeds the hook a PreToolUse payload on stdin and asserts on
the JSON it prints, with state and primers redirected away from the real
~/.claude.

Every rule is covered, plus the machinery around them: the decision
fold, criticality tiers, and per-rule isolation.
"""

import json
import os
import subprocess
import sys
import tempfile

HOOKS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HOOKS)
HOOK = os.path.join(HOOKS, "bash-policy.py")

FAILURES = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------- driver


def run_hook(command, cwd=None, home=None, session="testsession",
             transcript=None, isolate_git=False, agent=None):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session,
        "cwd": cwd or REPO,
    }
    if transcript:
        payload["transcript_path"] = transcript
    if agent:
        # Claude Code sends agent_id only from inside a subagent.
        payload["agent_id"] = agent
    env = dict(os.environ)
    env["BASH_POLICY_HOME"] = home or STATE_HOME
    env["BASH_POLICY_PRIMER_DIR"] = os.path.join(REPO, "hygiene")
    if isolate_git:
        # The developer's *global* commit.gpgsign would otherwise satisfy
        # the check, so "not configured anywhere" is untestable without
        # hiding the global and system config from the git the hook runs.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
    proc = subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )
    out = proc.stdout.strip()
    if not out:
        return {"decision": None, "reason": "", "context": "",
                "stderr": proc.stderr, "rc": proc.returncode}
    obj = json.loads(out)["hookSpecificOutput"]
    return {
        "decision": obj.get("permissionDecision"),
        "reason": obj.get("permissionDecisionReason") or "",
        "context": obj.get("additionalContext") or "",
        "stderr": proc.stderr,
        "rc": proc.returncode,
    }


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                   check=False)


def make_repo(gpgsign=None, commit=True, published=False):
    repo = tempfile.mkdtemp(prefix="bashpolicy-repo-")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    if gpgsign is not None:
        git(repo, "config", "commit.gpgsign", "true" if gpgsign else "false")
    if commit:
        git(repo, "commit", "--allow-empty", "--no-gpg-sign", "-q",
            "-m", "Seed the fixture repository")
    if published:
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


# ------------------------------------------------------- silence on misses

SILENT = [
    "echo hi",
    'echo "$HOME"',
    "echo $(date)",
    "ls -la",
    "git status",
    "git log --oneline -5",
    "git diff --stat",
    'F=~/tmp/x.txt; ls "$F"',
    'while read -r ln; do echo "${ln}"; done < /etc/hosts',
    'for F in a.txt b.txt; do echo "$F.bak"; done',
    "grep -r 'git commit' .",
    'echo "git push --force"',
]


def test_silence():
    for cmd in SILENT:
        r = run_hook(cmd)
        check(f"silent[{cmd[:38]}]", r["decision"] is None,
              f"got {r['decision']} / {r['reason'][:90]}")


# ------------------------------------------------------------- force push


def test_force_push():
    repo = make_repo(gpgsign=True, published=True)
    for cmd in ["git push --force", "git push -f origin main",
                "git push --force origin main"]:
        r = run_hook(cmd, cwd=repo)
        check(f"deny[{cmd}]", r["decision"] == "deny", f"got {r['decision']}")
        check(f"deny-reason[{cmd}]", "--force is not allowed" in r["reason"],
              r["reason"][:120])

    r = run_hook("git push origin +main:main", cwd=repo)
    check("deny[+refspec]", r["decision"] == "deny", f"got {r['decision']}")
    check("deny-reason[+refspec]", "'+refspec'" in r["reason"],
          r["reason"][:120])

    r = run_hook("git push --force-with-lease", cwd=repo)
    check("ask[lease]", r["decision"] == "ask", f"got {r['decision']}")
    check("ask-hint[lease]", "--force-if-includes" in r["reason"],
          r["reason"][:160])

    r = run_hook("git push --force-with-lease --force-if-includes", cwd=repo)
    check("ask[lease+includes]", r["decision"] == "ask", f"got {r['decision']}")
    check("ask-nohint[lease+includes]",
          "Add --force-if-includes" not in r["reason"], r["reason"][:160])

    # The tokenizer is permissive about an unbalanced quote, so the
    # force flag is still seen.
    r = run_hook("git push --force 'unterminated", cwd=repo)
    check("deny[unbalanced-quote force]", r["decision"] == "deny",
          f"got {r['decision']}")

    # Heredoc content is data, not a command.
    r = run_hook("cat <<'EOF'\ngit push --force\nEOF", cwd=repo)
    check("silent[force inside heredoc]", r["decision"] is None,
          f"got {r['decision']} / {r['reason'][:90]}")


# --------------------------------------------------------- commit signing


def test_commit_signing():
    repo = make_repo(gpgsign=True)
    r = run_hook('git commit --no-gpg-sign -m "Add a fixture"', cwd=repo)
    check("deny[--no-gpg-sign]", r["decision"] == "deny", f"got {r['decision']}")
    check("deny-reason[--no-gpg-sign]", "--no-gpg-sign" in r["reason"],
          r["reason"][:120])

    repo_off = make_repo(gpgsign=False)
    r = run_hook('git commit -m "Add a fixture"', cwd=repo_off)
    check("deny[gpgsign=false]", r["decision"] == "deny", f"got {r['decision']}")
    check("deny-reason[gpgsign=false]", "is disabled" in r["reason"],
          r["reason"][:120])

    repo_unset = make_repo(gpgsign=None)
    r = run_hook('git commit -m "Add a fixture"', cwd=repo_unset,
                 isolate_git=True)
    check("deny[gpgsign unset]", r["decision"] == "deny", f"got {r['decision']}")
    check("deny-reason[gpgsign unset]", "not configured" in r["reason"],
          r["reason"][:120])

    # git never answers when a gitopt cannot be resolved; that must not
    # read as "unsigned".
    r = run_hook('git -C "$R" commit -m "Add a fixture"', cwd=repo)
    check("no-deny[-C with an unexpanded variable]", r["decision"] != "deny",
          f"got {r['decision']} / {r['reason'][:120]}")

    # -S overrides the config check.
    r = run_hook('git commit -S -m "Add a fixture"', cwd=repo_unset,
                 isolate_git=True)
    check("allow[-S overrides unset]", r["decision"] == "allow",
          f"got {r['decision']} / {r['reason'][:120]}")

    # A repo with no local setting inherits the developer's global
    # commit.gpgsign. Only assert this where a global actually enables
    # signing.
    glob = subprocess.run(["git", "config", "--global", "--get",
                           "commit.gpgsign"],
                          capture_output=True, text=True, check=False)
    if glob.returncode == 0 and glob.stdout.strip().lower() == "true":
        r = run_hook('git commit -m "Add a fixture"', cwd=repo_unset)
        check("allow[gpgsign inherited from global]", r["decision"] == "allow",
              f"got {r['decision']} / {r['reason'][:120]}")


# ---------------------------------------------------- published rewriting


def test_published_rewrite():
    pub = make_repo(gpgsign=True, published=True)
    r = run_hook('git commit --amend -m "Add a fixture"', cwd=pub)
    check("ask[amend published]", r["decision"] == "ask", f"got {r['decision']}")
    check("ask-reason[amend published]", "already on a remote" in r["reason"],
          r["reason"][:140])

    r = run_hook("git rebase -i origin/main", cwd=pub)
    check("ask[rebase published]", r["decision"] == "ask", f"got {r['decision']}")

    unpub = make_repo(gpgsign=True, published=False)
    r = run_hook('git commit --amend -m "Add a fixture"', cwd=unpub)
    check("allow[amend unpublished]", r["decision"] == "allow",
          f"got {r['decision']} / {r['reason'][:120]}")

    r = run_hook("git rebase -i main", cwd=unpub)
    check("silent[rebase unpublished]", r["decision"] is None,
          f"got {r['decision']} / {r['reason'][:120]}")


# ------------------------------------------------------------ unsigned push


def test_unsigned_push():
    # Commits exist locally and are not on any remote ref: unsigned, so
    # pushing them is refused.
    repo = make_repo(gpgsign=True, published=False)
    r = run_hook("git push origin main", cwd=repo)
    check("deny[unsigned outgoing]", r["decision"] == "deny",
          f"got {r['decision']} / {r['reason'][:120]}")
    check("deny-reason[unsigned outgoing]", "is unsigned" in r["reason"],
          r["reason"][:140])

    # Nothing outgoing: nothing to check, and no allow is asserted.
    pub = make_repo(gpgsign=True, published=True)
    r = run_hook("git push origin main", cwd=pub)
    check("silent[nothing outgoing]", r["decision"] is None,
          f"got {r['decision']} / {r['reason'][:120]}")


# ------------------------------------------------------------ hygiene rules


def test_hygiene():
    repo = make_repo(gpgsign=True, published=True)
    long_subj = "Add " + "x" * 80
    cases = [
        (f'git commit -m "{long_subj}"', "deny", "limit ~70"),
        ('git commit -m "Add the thing."', "deny", "trailing period"),
        ('git commit -s -m "Add the thing"', "deny", "--signoff"),
        ('git commit -m "Add the thing" --trailer "Co-Authored-By: X <x@y.z>"',
         "deny", "trailer"),
        ('git commit -m "Add the thing" --trailer "Reviewed-by: X <x@y.z>"',
         "deny", "trailer"),
        ('git commit -m "Fix it, closes #1, #2"', "deny", "one closing keyword"),
        ('git commit -m "Add the thing"', "allow", ""),
    ]
    for cmd, want, needle in cases:
        r = run_hook(cmd, cwd=repo, session=f"hyg{abs(hash(cmd)) % 10**8}")
        check(f"hygiene[{needle or 'clean'}]", r["decision"] == want,
              f"want {want} got {r['decision']} / {r['reason'][:120]}")
        if needle:
            check(f"hygiene-reason[{needle}]", needle in r["reason"],
                  r["reason"][:160])

    # `git -C <path> commit` is normalized before matching, so hygiene
    # sees it.
    r = run_hook(f'git -C {repo} commit -m "Add the thing."',
                 cwd=repo, session="hygdashC")
    check("hygiene[git -C commit]", r["decision"] == "deny",
          f"got {r['decision']} / {r['reason'][:120]}")

    # gh PR bodies go through the same checks.
    r = run_hook('gh pr create --title "Add it" --body "Closes #1, #2"',
                 cwd=repo, session="hygpr")
    check("hygiene[pr closing-multi]", r["decision"] == "deny",
          f"got {r['decision']} / {r['reason'][:120]}")


def test_attribution():
    repo = make_repo(gpgsign=True, published=True)
    body = os.path.join(repo, "msg.txt")
    with open(body, "w") as fh:
        fh.write("Add the thing\n\nSome body text.\n\n"
                 "\U0001f916 Generated with Claude Code\n")
    r = run_hook(f"git commit --file {body}", cwd=repo, session="hygattr")
    check("hygiene[attribution]", r["decision"] == "deny",
          f"got {r['decision']} / {r['reason'][:120]}")
    check("hygiene-reason[attribution]", "attribution" in r["reason"].lower(),
          r["reason"][:160])


# ------------------------------------------------- fold across both domains


def test_fold():
    # Hygiene would allow (clean subject) but signing denies.
    repo_off = make_repo(gpgsign=False)
    r = run_hook('git commit -m "Add the thing"', cwd=repo_off, session="fold1")
    check("fold[clean msg + signing off -> deny]", r["decision"] == "deny",
          f"got {r['decision']}")

    # Both domains have something to say: one denial reports both.
    r = run_hook('git commit --no-gpg-sign -m "Add the thing."',
                 cwd=repo_off, session="fold2")
    check("fold[both -> deny]", r["decision"] == "deny", f"got {r['decision']}")
    check("fold[both reported]",
          "--no-gpg-sign" in r["reason"] and "trailing period" in r["reason"],
          r["reason"][:300])

    # deny outranks ask: amend on published HEAD (ask) plus a hygiene
    # violation (deny).
    pub = make_repo(gpgsign=True, published=True)
    r = run_hook('git commit --amend -m "Add the thing."', cwd=pub,
                 session="fold3")
    check("fold[deny outranks ask]", r["decision"] == "deny",
          f"got {r['decision']} / {r['reason'][:160]}")


# ------------------------------------------------- tiers and rule isolation


def test_tiers_in_process():
    sys.path.insert(0, HOOKS)
    from bashpolicy import shell
    from bashpolicy.policy import (ADVISORY, CRITICAL, Finding, Rule, evaluate)

    invs = shell.parse("git push origin main")

    def boom(invocations, ctx):
        raise RuntimeError("rule bug")

    critical = [Rule("boom-critical", CRITICAL, [("git", "push")], boom)]
    v = evaluate(invs, None, registry=critical)
    check("tier[critical raise -> deny]", v.decision == "deny",
          f"got {v.decision}")
    check("tier[critical reason names rule]", "boom-critical" in (v.reason or ""),
          (v.reason or "")[:120])

    advisory = [Rule("boom-advisory", ADVISORY, [("git", "push")], boom)]
    v = evaluate(invs, None, registry=advisory)
    check("tier[advisory raise -> silent]", v.decision is None,
          f"got {v.decision}")

    # An advisory bug must not suppress a critical rule in the same run.
    def deny_it(invocations, ctx):
        return [Finding("deny", "real-critical", msg="nope", tier=CRITICAL)]

    mixed = [Rule("boom-advisory", ADVISORY, [("git", "push")], boom),
             Rule("real-critical", CRITICAL, [("git", "push")], deny_it)]
    v = evaluate(invs, None, registry=mixed)
    check("tier[advisory bug does not mask critical]", v.decision == "deny",
          f"got {v.decision}")

    # Precedence ladder, order-independent.
    def mk(decision):
        return lambda invocations, ctx: [
            Finding(decision, decision, msg=decision, tier=ADVISORY)
        ]

    for order in (["allow", "ask", "deny"], ["deny", "ask", "allow"],
                  ["ask", "deny", "allow"]):
        reg = [Rule(d, ADVISORY, [("git", "push")], mk(d)) for d in order]
        v = evaluate(invs, None, registry=reg)
        check(f"fold[precedence {'>'.join(order)}]", v.decision == "deny",
              f"got {v.decision}")

    reg = [Rule(d, ADVISORY, [("git", "push")], mk(d)) for d in ("allow", "ask")]
    v = evaluate(invs, None, registry=reg)
    check("fold[ask outranks allow]", v.decision == "ask", f"got {v.decision}")

    # Matching without an explicit allow stays silent — this is what
    # keeps `git push` from being auto-approved.
    reg = [Rule("quiet", ADVISORY, [("git", "push")],
                lambda invocations, ctx: [])]
    v = evaluate(invs, None, registry=reg)
    check("fold[match without allow -> silent]",
          v.decision is None and v.matched, f"got {v.decision} {v.matched}")


def test_parser_in_process():
    sys.path.insert(0, HOOKS)
    from bashpolicy import shell

    invs = shell.parse("git -C /tmp/x commit -m hi")
    check("parse[git -C path normalized]", invs[0].path == ("git", "commit"),
          str(invs[0].path))
    check("parse[git -C gitopts kept]", invs[0].gitopts == ["-C", "/tmp/x"],
          str(invs[0].gitopts))

    invs = shell.parse("GIT_DIR=/tmp/y git push --force")
    check("parse[env prefix stripped]", invs[0].path == ("git", "push"),
          str(invs[0].path))

    invs = shell.parse("echo one && git push -f")
    check("parse[two segments]", len(invs) == 2, str([i.path for i in invs]))
    check("parse[second is push]", invs[1].path == ("git", "push"),
          str(invs[1].path))

    invs = shell.parse("gh pr create --title x --body y")
    check("parse[gh pr create]", invs[0].path == ("gh", "pr", "create"),
          str(invs[0].path))

    invs = shell.parse("cat <<'EOF'\ngit push --force\nEOF")
    check("parse[heredoc body not a segment]",
          all(i.path[:2] != ("git", "push") for i in invs),
          str([i.path for i in invs]))


# ------------------------------------------------------------------- primer

UNREADABLE_MARKER = "transcript could not be read"
DRIFT_MARKER = "no longer in the shape"


def transcript(tokens, assistant=True, path=None):
    """A transcript whose newest assistant turn reports `tokens` of
    context. `assistant=False` writes turns with no usage at all, which is
    what a format change would look like."""
    path = path or tempfile.mkstemp(prefix="bashpolicy-tr-", suffix=".jsonl")[1]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user"}}) + "\n")
        line = {"type": "assistant", "message": {"role": "assistant"}}
        if assistant:
            line["message"]["usage"] = {
                "input_tokens": 2,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": tokens - 2,
            }
        fh.write(json.dumps(line) + "\n")
    return path


def test_primer_token_rearm():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    cmd = 'git commit -m "Add the thing"'
    tr = transcript(50_000)

    first = run_hook(cmd, cwd=repo, home=home, session="ptok", transcript=tr)
    check("primer[first injects]", len(first["context"]) > 0,
          f"{len(first['context'])} chars")

    second = run_hook(cmd, cwd=repo, home=home, session="ptok", transcript=tr)
    check("primer[silent below the step]", second["context"] == "",
          f"{len(second['context'])} chars")

    transcript(50_000 + 199_000, path=tr)
    third = run_hook(cmd, cwd=repo, home=home, session="ptok", transcript=tr)
    check("primer[still silent just under the step]", third["context"] == "",
          f"{len(third['context'])} chars")

    transcript(50_000 + 200_000, path=tr)
    fourth = run_hook(cmd, cwd=repo, home=home, session="ptok", transcript=tr)
    check("primer[re-arms at the step]", len(fourth["context"]) > 0,
          f"{len(fourth['context'])} chars")

    check("primer[all allow]",
          {r["decision"] for r in (first, second, third, fourth)} == {"allow"},
          str([r["decision"] for r in (first, second, third, fourth)]))


def test_primer_rearms_on_drop():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    cmd = 'git commit -m "Add the thing"'
    tr = transcript(300_000)
    run_hook(cmd, cwd=repo, home=home, session="pdrop", transcript=tr)

    # A compaction: far less context than the marker recorded, and far
    # short of a step's growth.
    transcript(20_000, path=tr)
    after = run_hook(cmd, cwd=repo, home=home, session="pdrop", transcript=tr)
    check("primer[re-arms when the count drops]", len(after["context"]) > 0,
          f"{len(after['context'])} chars")


def test_primer_per_agent():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    cmd = 'git commit -m "Add the thing"'
    tr = transcript(50_000)

    sub = run_hook(cmd, cwd=repo, home=home, session="pagent", transcript=tr,
                   agent="agent-abc123")
    main = run_hook(cmd, cwd=repo, home=home, session="pagent", transcript=tr)
    check("primer[subagent injects]", len(sub["context"]) > 0,
          f"{len(sub['context'])} chars")
    check("primer[subagent does not consume the main thread's]",
          len(main["context"]) > 0, f"{len(main['context'])} chars")

    again = run_hook(cmd, cwd=repo, home=home, session="pagent", transcript=tr,
                     agent="agent-abc123")
    check("primer[subagent marker still holds]", again["context"] == "",
          f"{len(again['context'])} chars")


def test_primer_unreadable_count():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    cmd = 'git commit -m "Add the thing"'

    # No transcript_path at all: the first injection needs no count, so it
    # carries no complaint.
    first = run_hook(cmd, cwd=repo, home=home, session="pnone")
    check("primer[first injection is quiet about the count]",
          UNREADABLE_MARKER not in first["context"],
          first["context"][-120:])

    second = run_hook(cmd, cwd=repo, home=home, session="pnone")
    check("primer[reports an unreadable count]",
          UNREADABLE_MARKER in second["context"], second["context"][:160])

    third = run_hook(cmd, cwd=repo, home=home, session="pnone")
    check("primer[reports it only once]",
          UNREADABLE_MARKER not in third["context"],
          f"{len(third['context'])} chars")
    check("primer[no re-arm without a count]", third["context"] == "",
          f"{len(third['context'])} chars")


def test_primer_format_drift():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    cmd = 'git commit -m "Add the thing"'
    tr = transcript(50_000)
    run_hook(cmd, cwd=repo, home=home, session="pdrift", transcript=tr)

    # Assistant turns are there, but none carries a usage block.
    transcript(0, assistant=False, path=tr)
    after = run_hook(cmd, cwd=repo, home=home, session="pdrift", transcript=tr)
    check("primer[reports format drift, not an unreadable file]",
          DRIFT_MARKER in after["context"], after["context"][:160])


def test_primer_marker_migration():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    os.makedirs(os.path.join(home, ".state"), exist_ok=True)
    # What this scheme's predecessor left behind: a bare epoch.
    for cat in ("shared", "commit"):
        with open(os.path.join(home, ".state",
                               f"primer-pmig-main-{cat}"), "w") as fh:
            fh.write("1789464369")
    r = run_hook('git commit -m "Add the thing"', cwd=repo, home=home,
                 session="pmig", transcript=transcript(50_000))
    check("primer[pre-change marker re-arms once]", len(r["context"]) > 0,
          f"{len(r['context'])} chars")


def test_ack_and_retry():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    cmd = 'git commit -m "Add the thing for @someone"'
    first = run_hook(cmd, cwd=repo, home=home, session="ackretry")
    second = run_hook(cmd, cwd=repo, home=home, session="ackretry")
    check("ack[first denies]", first["decision"] == "deny",
          f"got {first['decision']} / {first['reason'][:120]}")
    check("ack[identical re-run passes]", second["decision"] == "allow",
          f"got {second['decision']} / {second['reason'][:120]}")


def test_non_bash_and_bad_input():
    env = dict(os.environ)
    env["BASH_POLICY_HOME"] = STATE_HOME
    for payload in [
        {"hook_event_name": "PreToolUse", "tool_name": "Read",
         "tool_input": {"file_path": "/etc/hosts"}},
        {"hook_event_name": "PostToolUse", "tool_name": "Bash",
         "tool_input": {"command": "git push --force"}},
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {}},
    ]:
        proc = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=60)
        check(f"ignore[{payload.get('hook_event_name')}/{payload.get('tool_name')}]",
              proc.stdout.strip() == "" and proc.returncode == 0,
              f"rc={proc.returncode} out={proc.stdout[:80]}")

    proc = subprocess.run([sys.executable, HOOK], input="not json at all",
                          capture_output=True, text=True, env=env, timeout=60)
    check("ignore[malformed stdin fails open]",
          proc.stdout.strip() == "" and proc.returncode == 0,
          f"rc={proc.returncode} out={proc.stdout[:80]}")


def stage(repo, name, text):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    git(repo, "add", name)


COMMENTED = """class Foo {
    // Jackson binds by field name, so these must not be renamed.
    // The keep rule in config.pro pins them.
    int bar;
}
"""

PLAIN = """class Plain {
    int bar;
}
"""

GUIDE_MARKER = "Three tests, in order"
REMINDER_MARKER = "apply the recovery, staleness and subject tests"


def test_comments():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-cmt-")
    stage(repo, "src/Foo.java", COMMENTED)
    cmd = 'git commit -m "Add the thing"'

    first = run_hook(cmd, cwd=repo, home=home, session="cmtfull")
    check("comments[full guide on first commit]",
          GUIDE_MARKER in first["context"],
          f"decision={first['decision']} ctx={first['context'][:160]}")
    check("comments[never denies]", first["decision"] == "allow",
          f"got {first['decision']} / {first['reason'][:120]}")

    second = run_hook(cmd, cwd=repo, home=home, session="cmtfull")
    check("comments[reminder on later commit]",
          REMINDER_MARKER in second["context"]
          and GUIDE_MARKER not in second["context"],
          f"ctx={second['context'][:200]}")
    check("comments[reminder points at the guide]",
          "primer-comments.md" in second["context"],
          f"ctx={second['context'][:200]}")
    check("comments[reminder counts the blocks]",
          "1 comment block" in second["context"],
          f"ctx={second['context'][:200]}")

    quiet = make_repo(gpgsign=True, published=True)
    stage(quiet, "src/Plain.java", PLAIN)
    r = run_hook(cmd, cwd=quiet, home=home, session="cmtplain")
    check("comments[silent without added comments]",
          GUIDE_MARKER not in r["context"],
          f"ctx={r['context'][:160]}")

    gen = make_repo(gpgsign=True, published=True)
    stage(gen, "generated/Gen.java", COMMENTED)
    r = run_hook(cmd, cwd=gen, home=home, session="cmtgen")
    check("comments[skips generated trees]",
          GUIDE_MARKER not in r["context"],
          f"ctx={r['context'][:160]}")

    # `git commit -a` stages at commit time, so the index is still empty
    # when the hook runs and only `git diff HEAD` sees the change.
    dash_a = make_repo(gpgsign=True, published=True)
    stage(dash_a, "src/Foo.java", PLAIN)
    git(dash_a, "commit", "--no-gpg-sign", "-q", "-m", "Seed the source file")
    with open(os.path.join(dash_a, "src/Foo.java"), "w", encoding="utf-8") as fh:
        fh.write(COMMENTED)
    r = run_hook('git commit -am "Add the thing"', cwd=dash_a, home=home,
                 session="cmtdasha")
    check("comments[sees -a unstaged changes]", GUIDE_MARKER in r["context"],
          f"ctx={r['context'][:160]}")


def test_comment_blocks_in_process():
    sys.path.insert(0, HOOKS)
    from bashpolicy.comments import _blocks_by_file

    diff = ("+++ b/src/Foo.java\n"
            "@@ -1,0 +2,2 @@\n"
            "+    // one\n"
            "+    // two\n"
            "@@ -20,0 +30,1 @@\n"
            "+    // far away\n")
    check("blocks[hunk header splits a run]",
          _blocks_by_file(diff) == {"src/Foo.java": 2},
          str(_blocks_by_file(diff)))

    javadoc = ("+++ b/src/Foo.java\n"
               "@@ -1,0 +2,3 @@\n"
               "+    /**\n"
               "+     * Why not the obvious alternative.\n"
               "+     */\n"
               "+    int bar;\n")
    check("blocks[javadoc counts as one block]",
          _blocks_by_file(javadoc) == {"src/Foo.java": 1},
          str(_blocks_by_file(javadoc)))

    mixed = ("+++ b/node_modules/dep/index.js\n"
             "@@ -1,0 +2,1 @@\n"
             "+// vendored\n"
             "+++ b/scripts/run.sh\n"
             "@@ -1,0 +2,2 @@\n"
             "+#!/bin/sh\n"
             "+# a real comment\n")
    check("blocks[skips vendored, ignores shebang]",
          _blocks_by_file(mixed) == {"scripts/run.sh": 1},
          str(_blocks_by_file(mixed)))

    unmapped = ("+++ b/notes.txt\n"
                "@@ -1,0 +2,1 @@\n"
                "+# not a code comment\n")
    check("blocks[unmapped suffix yields nothing]",
          _blocks_by_file(unmapped) == {},
          str(_blocks_by_file(unmapped)))


# ------------------------------------------------ worktree / branch naming


NAMING_SILENT = [
    # An operand the shell would rewrite is never judged.
    'git worktree add "$WT"',
    'git switch -c "$B"',
    'git checkout -b "${BRANCH}"',
    'git worktree add ../wt-$(date +%s)',
    # Conforming names.
    "git checkout -b sbaghino/123-do-the-thing",
    "git switch -c sbaghino/do-the-thing",
    "git branch sbaghino/9-x",
    "git worktree add -b sbaghino/1-work ../wt",
    "git branch -m sbaghino/1-a sbaghino/2-b",
    # Not a creation at all.
    "git branch -d sbaghino/1-old",
    "git branch -a",
    "git branch --list 'sbaghino/*'",
    "git branch -v",
    "git branch --set-upstream-to=origin/main sbaghino/1-a",
    "git checkout main",
    "git switch main",
    "git worktree add ../wt sbaghino/1-work",
    "git worktree add --detach ../wt",
    "git worktree list",
]


def test_naming():
    repo = make_repo(gpgsign=True)

    r = run_hook("git worktree add ../wt+1", cwd=repo)
    check("deny[worktree +]", r["decision"] == "deny", f"got {r['decision']}")
    check("deny-reason[worktree +]",
          "`+`" in r["reason"] and "Gradle" in r["reason"], r["reason"][:160])

    r = run_hook("git worktree add -b sbaghino/1-work ../wt+1", cwd=repo)
    check("deny[worktree + with good branch]", r["decision"] == "deny",
          f"got {r['decision']}")

    r = run_hook("git worktree add -b sbaghino/1-work+x ../wt", cwd=repo)
    check("deny[worktree branch +]", r["decision"] == "deny",
          f"got {r['decision']}")

    # No commit-ish and no -b: git names the branch after the path's last
    # segment, which can never carry the required `sbaghino/` prefix.
    r = run_hook("git worktree add ../wt", cwd=repo)
    check("deny[worktree implied branch]", r["decision"] == "deny",
          f"got {r['decision']}")
    check("deny-reason[worktree implied branch]", "-b" in r["reason"],
          r["reason"][:200])

    for cmd, bad in [("git checkout -b feature/foo", "feature/foo"),
                     ("git switch --create=Bad_Name", "Bad_Name"),
                     ("git switch -c sbaghino/Has-Caps", "sbaghino/Has-Caps"),
                     ("git branch sbaghino/trailing-", "sbaghino/trailing-"),
                     ("git branch nopfx", "nopfx"),
                     ("git branch -m sbaghino/1-a BAD", "BAD"),
                     ("git checkout -b sbaghino/a--b", "sbaghino/a--b")]:
        r = run_hook(cmd, cwd=repo)
        check(f"deny[{cmd}]", r["decision"] == "deny", f"got {r['decision']}")
        check(f"deny-reason[{cmd}]", f"`{bad}`" in r["reason"],
              r["reason"][:160])

    for cmd in NAMING_SILENT:
        r = run_hook(cmd, cwd=repo)
        check(f"naming-silent[{cmd[:40]}]", r["decision"] is None,
              f"got {r['decision']} / {r['reason'][:100]}")

    # Matching a naming prefix must not auto-approve the command.
    r = run_hook("git checkout -b sbaghino/1-fine", cwd=repo)
    check("naming[conforming name is not an allow]", r["decision"] is None,
          f"got {r['decision']}")

    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    with open(os.path.join(home, "hygiene-config.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"branch_name_pattern": "^wip/[a-z]+$"}, fh)
    r = run_hook("git checkout -b wip/thing", cwd=repo, home=home)
    check("naming[config pattern accepts]", r["decision"] is None,
          f"got {r['decision']} / {r['reason'][:120]}")
    r = run_hook("git checkout -b sbaghino/1-thing", cwd=repo, home=home)
    check("naming[config pattern rejects the default shape]",
          r["decision"] == "deny", f"got {r['decision']}")


# --------------------------------------------------------------------- main

if __name__ == "__main__":
    STATE_HOME = tempfile.mkdtemp(prefix="bashpolicy-home-")
    test_silence()
    test_force_push()
    test_commit_signing()
    test_published_rewrite()
    test_unsigned_push()
    test_hygiene()
    test_attribution()
    test_fold()
    test_tiers_in_process()
    test_parser_in_process()
    test_primer_token_rearm()
    test_primer_rearms_on_drop()
    test_primer_per_agent()
    test_primer_unreadable_count()
    test_primer_format_drift()
    test_primer_marker_migration()
    test_ack_and_retry()
    test_comments()
    test_naming()
    test_comment_blocks_in_process()
    test_non_bash_and_bad_input()

    print(f"passed: {PASSED}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS")
