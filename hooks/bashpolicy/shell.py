"""Shell command parsing shared by every policy rule.

Lifted from the quote/heredoc-aware splitter that `hygiene-dispatch.py`
grew, so the git-history rules inherit it instead of the `shlex.split`
approximation they used to carry — that one could not see into heredocs
and silently allowed any command it failed to parse.

Ambiguity resolves toward fewer segments: false negatives are acceptable
here, false positives are not.
"""

import re

GIT_OPTS_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace")


class ParseError(Exception):
    """The command could not be parsed well enough to police it."""


def split_top_level(cmd):
    """Split a shell command into top-level segments.

    Tracks single/double quotes, backslash escapes, $()/backtick nesting
    and heredocs. Returns [(segment_text, [heredoc_bodies])]. Heredoc
    bodies are attached to the segment whose redirection introduced them.
    """
    segs = []
    cur = []
    cur_heredocs = []
    pending_heredocs = []  # (delimiter, strip_tabs) awaiting their bodies
    i, n = 0, len(cmd)
    in_sq = in_dq = False
    depth = 0  # $( ) nesting; segments never split inside

    def flush():
        nonlocal cur, cur_heredocs
        text = "".join(cur).strip()
        if text:
            segs.append((text, cur_heredocs))
        cur, cur_heredocs = [], []

    while i < n:
        c = cmd[i]
        if in_sq:
            cur.append(c)
            if c == "'":
                in_sq = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            cur.append(c)
            cur.append(cmd[i + 1])
            i += 2
            continue
        if in_dq:
            cur.append(c)
            if c == '"':
                in_dq = False
            elif c == "$" and i + 1 < n and cmd[i + 1] == "(":
                depth += 1
                cur.append("(")
                i += 1
            elif c == ")" and depth > 0:
                depth -= 1
            i += 1
            continue
        if c == "'":
            in_sq = True
            cur.append(c)
            i += 1
            continue
        if c == '"':
            in_dq = True
            cur.append(c)
            i += 1
            continue
        if c == "$" and i + 1 < n and cmd[i + 1] == "(":
            depth += 1
            cur.append("$(")
            i += 2
            continue
        if c == ")" and depth > 0:
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c == "<" and cmd[i : i + 2] == "<<" and cmd[i : i + 3] != "<<<":
            j = i + 2
            strip_tabs = False
            if j < n and cmd[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and cmd[j] in " \t":
                j += 1
            m = re.match(r"""(['"]?)([A-Za-z0-9_]+)\1""", cmd[j:])
            if m:
                pending_heredocs.append((m.group(2), strip_tabs))
                cur.append(cmd[i : j + m.end()])
                i = j + m.end()
                continue
            cur.append(c)
            i += 1
            continue
        if c == "\n" and pending_heredocs and depth == 0:
            # consume heredoc bodies; they are not command segments
            cur.append(c)
            i += 1
            while pending_heredocs and i < n:
                delim, strip_tabs = pending_heredocs.pop(0)
                body_lines = []
                while i < n:
                    nl = cmd.find("\n", i)
                    line = cmd[i:nl] if nl != -1 else cmd[i:]
                    i = (nl + 1) if nl != -1 else n
                    check = line.lstrip("\t") if strip_tabs else line
                    if check == delim:
                        break
                    body_lines.append(line)
                cur_heredocs.append("\n".join(body_lines))
            continue
        if depth == 0 and not pending_heredocs:
            if c == "\n" or c == ";":
                flush()
                i += 1
                continue
            if cmd[i : i + 2] in ("&&", "||"):
                flush()
                i += 2
                continue
            if c in "|&":
                flush()
                i += 1
                continue
        cur.append(c)
        i += 1
    flush()
    return segs


ASSIGN_RE = re.compile(
    r"""^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|"(?:\\.|[^"\\])*"|[^\s'"]*)\s+"""
)


def strip_assignments(seg):
    """Drop leading `VAR=value` prefixes, so `GIT_DIR=x git push` is
    still seen as a `git push`."""
    while True:
        m = ASSIGN_RE.match(seg)
        if not m:
            return seg
        seg = seg[m.end() :]


def tokenize(seg):
    """Best-effort shell word split honoring quotes and $() nesting.
    Returns unquoted word values."""
    words = []
    cur = []
    started = False
    i, n = 0, len(seg)
    in_sq = in_dq = False
    depth = 0
    while i < n:
        c = seg[i]
        if in_sq:
            if c == "'":
                in_sq = False
            else:
                cur.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n and not in_sq:
            cur.append(seg[i + 1])
            started = True
            i += 2
            continue
        if in_dq:
            if c == '"' and depth == 0:
                in_dq = False
            else:
                if c == "$" and seg[i : i + 2] == "$(":
                    depth += 1
                    cur.append("$(")
                    i += 2
                    continue
                if c == ")" and depth > 0:
                    depth -= 1
                cur.append(c)
            i += 1
            continue
        if c == "'":
            in_sq = True
            started = True
            i += 1
            continue
        if c == '"':
            in_dq = True
            started = True
            i += 1
            continue
        if c == "$" and seg[i : i + 2] == "$(":
            depth += 1
            cur.append("$(")
            started = True
            i += 2
            continue
        if c == ")" and depth > 0:
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if c in " \t" and depth == 0:
            if started or cur:
                words.append("".join(cur))
            cur = []
            started = False
            i += 1
            continue
        cur.append(c)
        started = True
        i += 1
    if started or cur:
        words.append("".join(cur))
    return words


class Invocation:
    """One top-level command segment, parsed once and shared by all rules.

    `path` is the (tool, subcommand, ...) prefix rules match on. git's
    global options are skipped first, so `git -C /x commit` has path
    ("git", "commit") and gitopts ["-C", "/x"] — `hygiene-dispatch.py`
    matched on raw leading words and so missed that form entirely.
    """

    __slots__ = ("seg", "heredocs", "words", "gitopts", "path", "argv")

    def __init__(self, seg, heredocs):
        self.seg = seg
        self.heredocs = heredocs
        stripped = strip_assignments(seg.lstrip())
        try:
            self.words = tokenize(stripped)
        except Exception as exc:  # tokenizer is best-effort; treat as opaque
            raise ParseError(str(exc)) from exc
        self.gitopts = []
        self.path = ()
        self.argv = []
        if not self.words:
            return
        tool = self.words[0]
        i = 1
        if tool == "git":
            while i < len(self.words):
                tok = self.words[i]
                if tok in GIT_OPTS_WITH_VALUE:
                    self.gitopts.extend(self.words[i : i + 2])
                    i += 2
                elif tok.startswith("-"):
                    self.gitopts.append(tok)
                    i += 1
                else:
                    break
        subs = []
        j = i
        while j < len(self.words) and len(subs) < 2:
            tok = self.words[j]
            if tok.startswith("-"):
                break
            subs.append(tok)
            j += 1
        self.path = (tool, *subs)
        self.argv = self.words[i:]

    def matches(self, prefix):
        """True when this invocation starts with `prefix`, e.g.
        ("gh", "pr", "create") or ("git", "push")."""
        prefix = tuple(prefix)
        return self.path[: len(prefix)] == prefix

    def opts(self, subcommand_words):
        """argv with the leading subcommand words removed, leaving the
        flags and positionals a rule cares about."""
        return self.argv[subcommand_words:]


def parse(cmd):
    """Parse a full command string into Invocations. Raises ParseError."""
    try:
        segments = split_top_level(cmd)
    except Exception as exc:
        raise ParseError(str(exc)) from exc
    return [Invocation(seg, heredocs) for seg, heredocs in segments]
