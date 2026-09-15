"""Git history-safety rules: no rewriting published history, no unsigned
commits.

These rules are CRITICAL: a bug in them denies rather than letting a
force push through. Signature checking is a single `git log --format`
call over at most MAX_SCANNED_COMMITS outgoing commits, and git runs
with an explicit cwd from the hook payload rather than inheriting
whatever directory the hook process happens to sit in.

Only `%G?` == "N" counts as unsigned: a commit whose signature exists
but cannot be verified locally (no public key — `%G?` == "E") is signed,
and denying those would block every push on a machine without the
signer's key.
"""

import subprocess

from .policy import CRITICAL, Finding, rule

MAX_SCANNED_COMMITS = 100
PER_CALL_TIMEOUT = 15


class BudgetExhausted(Exception):
    """The aggregate git-inspection budget ran out."""


def git(ctx, args):
    """Run git under both a per-call and an aggregate time budget."""
    remaining = ctx.remaining_budget()
    if remaining <= 0:
        raise BudgetExhausted("git inspection budget exhausted")
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=ctx.cwd or None,
        timeout=min(PER_CALL_TIMEOUT, remaining),
    )


def head_published(ctx, verb):
    r = git(ctx, ["branch", "-r", "--contains", "HEAD"])
    if r.returncode == 0 and r.stdout.strip():
        return Finding(
            "ask", "git-published-rewrite", tier=CRITICAL,
            msg=(f"HEAD is already on a remote branch; {verb} would rewrite "
                 "published history. The default is a follow-up commit instead. "
                 "Proceed only for the rare cases that justify a rewrite (e.g. "
                 "replacing an unsigned commit that breaks CI), and state the "
                 "reason."),
        )
    return None


@rule("git-force-push", CRITICAL, [("git", "push")])
def check_force_push(invocations, ctx):
    """Pure string inspection — no git calls, so it holds even when the
    repository cannot be interrogated."""
    findings = []
    for inv in invocations:
        rest = inv.opts(1)
        positional = [t for t in rest if not t.startswith("-")]
        for tok in rest:
            if tok in ("-f", "--force"):
                findings.append(Finding(
                    "deny", "git-force-push", tier=CRITICAL,
                    msg=("git push --force is not allowed: it can discard commits "
                         "pushed by others. If rewriting published history is "
                         "genuinely required, use --force-with-lease "
                         "--force-if-includes and state the reason; the user will "
                         "be asked to approve."),
                ))
                break
        for tok in positional[1:]:
            if tok.startswith("+"):
                findings.append(Finding(
                    "deny", "git-force-push", tier=CRITICAL,
                    msg=("A '+refspec' is a force push and is not allowed. If "
                         "rewriting published history is genuinely required, use "
                         "--force-with-lease --force-if-includes and state the "
                         "reason."),
                ))
                break
        lease = [t for t in rest
                 if t == "--force-with-lease" or t.startswith("--force-with-lease=")]
        if lease:
            includes = "--force-if-includes" in rest
            hint = ("" if includes else
                    " Add --force-if-includes so a stale remote-tracking ref "
                    "cannot be clobbered.")
            findings.append(Finding(
                "ask", "git-force-push-lease", tier=CRITICAL,
                msg=("This is a force push (--force-with-lease), which rewrites "
                     "published history. It should only proceed for a good reason "
                     "the user agrees with." + hint),
            ))
    return findings


@rule("git-unsigned-push", CRITICAL, [("git", "push")])
def check_unsigned_push(invocations, ctx):
    """Refuse to push commits with no signature at all."""
    inv = invocations[0]
    r = git(ctx, [*inv.gitopts, "log", f"--max-count={MAX_SCANNED_COMMITS}",
                  "--format=%H %G?", "HEAD", "--not", "--remotes"])
    if r.returncode != 0:
        return []  # unborn HEAD, not a repo, no remotes configured
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, sig = parts
        if sig == "N":
            return [Finding(
                "deny", "git-unsigned-push", tier=CRITICAL,
                msg=(f"Refusing to push: commit {sha[:12]} is unsigned and would "
                     "break CI. Re-create it signed (git config commit.gpgsign "
                     "true, or commit -S) before pushing."),
            )]
    return []


@rule("git-commit-signing", CRITICAL, [("git", "commit")])
def check_commit_signing(invocations, ctx):
    findings = []
    for inv in invocations:
        rest = inv.opts(1)
        if "--no-gpg-sign" in rest:
            findings.append(Finding(
                "deny", "git-commit-signing", tier=CRITICAL,
                msg=("Unsigned commits are not allowed (--no-gpg-sign). Commit "
                     "without that flag so the commit is signed."),
            ))
            continue
        signed_flag = any(
            tok == "--gpg-sign" or tok.startswith("--gpg-sign=")
            or tok == "-S" or (tok.startswith("-S") and not tok.startswith("-S-"))
            for tok in rest
        )
        if signed_flag:
            continue
        r = git(ctx, [*inv.gitopts, "config", "--get", "commit.gpgsign"])
        if r.returncode == 0 and r.stdout.strip().lower() == "false":
            findings.append(Finding(
                "deny", "git-commit-signing", tier=CRITICAL,
                msg=("commit.gpgsign is disabled in this repo, so this commit "
                     "would be unsigned. Enable signing or pass -S explicitly."),
            ))
        elif r.returncode == 1:
            # `config --get` exits 1 only for an unset key. A different
            # status means git never answered, as it does for a gitopt the
            # parser cannot resolve such as `-C "$REPO"`; denying then
            # would block a correctly signed commit.
            findings.append(Finding(
                "deny", "git-commit-signing", tier=CRITICAL,
                msg=("commit.gpgsign is not configured, so this commit would be "
                     "unsigned. Enable signing (git config commit.gpgsign true) "
                     "or pass -S explicitly."),
            ))
    return findings


@rule("git-published-rewrite", CRITICAL, [("git", "commit"), ("git", "rebase")])
def check_published_rewrite(invocations, ctx):
    findings = []
    for inv in invocations:
        if inv.matches(("git", "commit")):
            if "--amend" not in inv.opts(1):
                continue
            verb = "--amend"
        else:
            verb = "rebase"
        f = head_published(ctx, verb)
        if f:
            findings.append(f)
            break  # one ask is enough
    return findings
