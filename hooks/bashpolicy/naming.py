"""Worktree and branch naming conventions (ADVISORY).

Two rules in one module: both police a name the command is about to
create.

  * `+` anywhere in a worktree path interferes with Gradle.
  * Branches are `sbaghino/[<issue-number>-]<kebab-case-blurb>`.

Neither can be settled from the command string alone. `git worktree add
"$WT"` reaches the hook as the literal four characters `$WT`, and no
shell expands that without also running whatever command substitutions
the string holds — `bash -n` and `zsh -n` expand nothing, a DEBUG trap
reports the command still unexpanded, and `set -x` prints the expanded
form only after executing the substitution. So the rules here judge what
is literally spelled out and stay silent on the rest, the way the
signing rule does for a `-C` it cannot resolve.

A name behind an expansion is therefore an accepted false negative, the
same trade the matcher already makes for a watched command wrapped in
`$(...)`.
"""

import os
import re

from .policy import ADVISORY, Finding, rule

BRANCH_PATTERN = r"^sbaghino/(?:\d+-)?[a-z0-9]+(?:-[a-z0-9]+)*$"
BRANCH_SHAPE = "sbaghino/[<issue-number>-]<kebab-case-blurb>"

# Every spelling that creates a worktree or a branch.
CREATE_PREFIXES = [("git", "worktree", "add"), ("git", "branch"),
                   ("git", "checkout"), ("git", "switch")]

WORKTREE_VALUE_FLAGS = ("-b", "-B", "--reason")
CHECKOUT_CREATE = ("-b", "-B")
SWITCH_CREATE = ("-c", "-C", "--create", "--force-create")

# `git branch` does a dozen things; these say creating is not one of them.
BRANCH_NOT_CREATING = (
    "--delete", "--list", "--remotes", "--all", "--show-current",
    "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--format", "--sort", "--column", "--edit-description",
    "--set-upstream-to", "--unset-upstream", "--verbose",
)
# Bundled shorts are not split by the parser, so `-dr` has to match as
# well as `-d`. The single leading dash keeps `--delete` out.
BRANCH_SHORT_NOT_CREATING = re.compile(r"^-[a-zA-Z]*[dDlravu][a-zA-Z]*$")
BRANCH_RENAMING = ("-m", "-M", "-c", "-C", "--move", "--copy")


def _unresolved(tok):
    """True when the shell would rewrite this token before git sees it.
    Such a token is never judged: see the module docstring."""
    return "$" in tok or "`" in tok


def _has(toks, flag):
    return any(t == flag or (flag.startswith("--") and t.startswith(flag + "="))
               for t in toks)


def _flag_value(toks, flags):
    """The value of the first of `flags` present, as `--flag=value` or
    `--flag value`, or None when the flag is absent or has no value."""
    for i, tok in enumerate(toks):
        for f in flags:
            if tok == f:
                return toks[i + 1] if i + 1 < len(toks) else None
            if f.startswith("--") and tok.startswith(f + "="):
                return tok[len(f) + 1:]
    return None


def _positionals(toks, value_flags):
    out = []
    skip = False
    for tok in toks:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            skip = tok in value_flags
            continue
        out.append(tok)
    return out


# ------------------------------------------------------------ what is made


def worktree_names(inv):
    """Every name `git worktree add` would create: the path, and the
    branch when `-b`/`-B` names one."""
    toks = inv.opts(2)
    names = _positionals(toks, WORKTREE_VALUE_FLAGS)[:1]
    branch = _flag_value(toks, ("-b", "-B"))
    return names + ([branch] if branch else [])


def created_branch(inv):
    """`(name, path)` for the branch this invocation creates.

    `path` is set only when git derives the name from a worktree path
    rather than being handed it, which changes the advice. `(None, None)`
    when the invocation creates no branch.
    """
    if inv.matches(("git", "worktree", "add")):
        toks = inv.opts(2)
        named = _flag_value(toks, ("-b", "-B"))
        if named:
            return named, None
        if _has(toks, "--detach"):
            return None, None
        pos = _positionals(toks, WORKTREE_VALUE_FLAGS)
        # With no commit-ish and no -b, git creates a branch as if
        # `-b $(basename <path>)` had been given.
        if len(pos) == 1:
            path = pos[0].rstrip("/")
            return os.path.basename(path), pos[0]
        return None, None

    if inv.matches(("git", "checkout")):
        return _flag_value(inv.opts(1), CHECKOUT_CREATE), None

    if inv.matches(("git", "switch")):
        return _flag_value(inv.opts(1), SWITCH_CREATE), None

    if inv.matches(("git", "branch")):
        toks = inv.opts(1)
        if any(_has(toks, f) for f in BRANCH_NOT_CREATING):
            return None, None
        if any(BRANCH_SHORT_NOT_CREATING.match(t) for t in toks):
            return None, None
        pos = _positionals(toks, ())
        if not pos:
            return None, None
        if any(_has(toks, f) for f in BRANCH_RENAMING):
            return pos[-1], None
        return pos[0], None

    return None, None


# ------------------------------------------------------------- the pattern


def branch_re(config):
    raw = (config or {}).get("branch_name_pattern") or BRANCH_PATTERN
    try:
        return re.compile(raw)
    except re.error:
        return re.compile(BRANCH_PATTERN)


def branch_problem(name, config):
    """The reason `name` is not an acceptable branch name, or None."""
    if branch_re(config).match(name):
        return None
    return (f"Branch name `{name}` does not match the convention "
            f"`{BRANCH_SHAPE}`: a `sbaghino/` prefix, an optional issue "
            "number, then a lowercase hyphen-separated blurb.")


# --------------------------------------------------------------- the rules


@rule("worktree-name", ADVISORY, [("git", "worktree", "add")])
def check_worktree_name(invocations, ctx):
    findings = []
    for inv in invocations:
        for name in worktree_names(inv):
            if _unresolved(name) or "+" not in name:
                continue
            findings.append(Finding(
                "deny", "worktree-name",
                msg=(f"`{name}` carries a `+`, which interferes with Gradle. "
                     "Choose a worktree path and branch without one."),
            ))
            break
    return findings


@rule("branch-name", ADVISORY, CREATE_PREFIXES)
def check_branch_name(invocations, ctx):
    findings = []
    for inv in invocations:
        name, path = created_branch(inv)
        if not name or _unresolved(name):
            continue
        problem = branch_problem(name, ctx.config)
        if not problem:
            continue
        if path:
            problem += (f" git derives it from `{path}` because the command "
                        "gives neither a commit-ish nor `-b`; pass `-b "
                        "<name>` explicitly.")
        findings.append(Finding("deny", "branch-name", msg=problem))
    return findings
