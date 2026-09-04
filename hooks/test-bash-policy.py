#!/usr/bin/env python3
"""Unit tests for hooks/bash-policy.py — no dependencies, no live session.

Run: python3 hooks/test-bash-policy.py

Complements hooks/verify-hygiene-hooks.sh, which drives a real headless
Claude Code session (and costs one) to confirm the hook is wired up and
stays quiet on tricky quoting. This suite exercises the decisions
directly: it feeds the hook a PreToolUse payload on stdin and asserts on
the JSON it prints, with state and primers redirected away from the real
~/.claude.

Every case that mattered to either predecessor hook is covered, plus the
behaviors the merge introduces: the decision fold, criticality tiers,
and per-rule isolation.
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
             transcript=None, isolate_git=False):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session,
        "cwd": cwd or REPO,
    }
    if transcript:
        payload["transcript_path"] = transcript
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

    # An unbalanced quote used to make shlex.split raise, and the old
    # guard returned (allowing) on that. The shared tokenizer is
    # permissive, so the force flag is still seen.
    r = run_hook("git push --force 'unterminated", cwd=repo)
    check("deny[unbalanced-quote force]", r["decision"] == "deny",
          f"got {r['decision']}")

    # Heredoc content is not a command: the old parser could not see the
    # difference, this one can.
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

    # -S overrides the config check.
    r = run_hook('git commit -S -m "Add a fixture"', cwd=repo_unset,
                 isolate_git=True)
    check("allow[-S overrides unset]", r["decision"] == "allow",
          f"got {r['decision']} / {r['reason'][:120]}")

    # A repo with no local setting inherits the developer's global
    # commit.gpgsign, which is what the old guard did too. Only assert
    # this where a global actually enables signing.
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
    # now sees it. The old raw leading-word match did not.
    r = run_hook(f'git -C {repo} commit -m "Add the thing."',
                 cwd=repo, session="hygdashC")
    check("hygiene[git -C commit]", r["decision"] == "deny",
          f"got {r['decision']} / {r['reason'][:120]}")

    # gh PR body checks still fire.
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
    # keeps `git push` from being auto-approved now that the history
    # rules share a hook with hygiene.
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


def test_primer_once_per_session():
    repo = make_repo(gpgsign=True, published=True)
    home = tempfile.mkdtemp(prefix="bashpolicy-home-")
    first = run_hook('git commit -m "Add the thing"', cwd=repo, home=home,
                     session="primerone")
    second = run_hook('git commit -m "Add another thing"', cwd=repo, home=home,
                      session="primerone")
    check("primer[first injects]", len(first["context"]) > 0,
          f"{len(first['context'])} chars")
    check("primer[second silent]", second["context"] == "",
          f"{len(second['context'])} chars")
    check("primer[both allow]",
          first["decision"] == "allow" and second["decision"] == "allow",
          f"{first['decision']} / {second['decision']}")


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
    test_primer_once_per_session()
    test_ack_and_retry()
    test_non_bash_and_bad_input()

    print(f"passed: {PASSED}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS")
