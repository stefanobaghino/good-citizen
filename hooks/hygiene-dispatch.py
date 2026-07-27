#!/usr/bin/env python3
"""Hygiene dispatcher — Claude Code PreToolUse hook for Bash.

Single registration (matcher "Bash", no `if`) replacing the five if-gated
`hygiene.sh` entries this repo used to ship. See README.md.

Design: enforce, don't instruct. Mechanically checkable hygiene rules are
validated against the drafted commit message / PR / issue body at action
time and violations are denied with the exact fix; context injection is
reduced to a small per-category primer for judgment-only rules.

Behavior:
  - Matches `git commit`, `gh pr create`, `gh issue create|comment|edit`
    at the start of top-level command segments only (quote/heredoc-aware
    splitting; content inside $(...) or quotes never matches).
  - Non-matching commands: no output, exit 0. `permissionDecision: allow`
    is emitted only on validated matches, never as a blanket.
  - Tier A (deterministic) and Tier B (high-confidence heuristic)
    violations deny with all findings at once. Tier C (intent-dependent:
    bare @mentions / #refs / hex tokens) denies once and passes when the
    same token set is re-submitted (ack-and-retry).
  - Loop guard: after 2 denies for the same (session, kinds) without the
    violation set shrinking, fails open — allow + findings as a warning.
  - Primer: judgment-only guidance injected at most once per session per
    category. Re-arm: marker older than PRIMER_TTL (4 h), or transcript
    gained a compact_boundary newer than the marker.
  - Fail open: any *error* path exits 0 with no output; only documented
    rule violations may deny.

Layout:
  Primers: <this repo>/hygiene/primer-<cat>.md  (resolved relative to
           this script, so the repo stays self-contained)
  State:   ~/.claude/hooks/.state/   (markers; entries older than 7 days
           are opportunistically removed)
  Config:  ~/.claude/hooks/hygiene-config.json
           {"signoff_cwd_substrings": ["/path/fragment", ...]}
           cwd substrings of repos whose contribution guide requires
           Signed-off-by (suppresses the trailer denial there).
"""

import hashlib
import json
import os
import re
import sys
import time

BASE = os.path.expanduser("~/.claude/hooks")
STATE_DIR = os.path.join(BASE, ".state")
CONFIG_PATH = os.path.join(BASE, "hygiene-config.json")
# Primers ship with the repo, next to this script's parent `hygiene/` dir.
PRIMER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hygiene")
)

PRIMER_TTL = 4 * 3600
STATE_MAX_AGE = 7 * 24 * 3600
INLINE_BODY_LIMIT = 200
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024

COMMANDS = {
    ("git", "commit"): "commit",
    ("gh", "pr", "create"): "pr",
    ("gh", "pr", "edit"): "pr",
    ("gh", "pr", "comment"): "pr",
    ("gh", "issue", "create"): "issue",
    ("gh", "issue", "comment"): "issue",
    ("gh", "issue", "edit"): "issue",
}

# ---------------------------------------------------------------- splitting


def split_top_level(cmd):
    """Split a shell command into top-level segments.

    Tracks single/double quotes, backslash escapes, $()/backtick nesting
    and heredocs. Returns [(segment_text, [heredoc_bodies])]. Heredoc
    bodies are attached to the segment whose redirection introduced them.
    No full shell parsing — ambiguity resolves toward fewer segments
    (false negatives are acceptable, false positives are not).
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
    while True:
        m = ASSIGN_RE.match(seg)
        if not m:
            return seg
        seg = seg[m.end() :]


def match_kind(seg):
    """Return (kind, subcommand-tuple) if the segment starts with a
    watched command as whole words, else (None, None)."""
    seg = strip_assignments(seg.lstrip())
    words = seg.split()
    for prefix, kind in COMMANDS.items():
        if tuple(words[: len(prefix)]) == prefix:
            return kind, prefix
    return None, None


# ------------------------------------------------------------- extraction


def tokenize(seg):
    """Best-effort shell word split honoring quotes and $() nesting.
    Returns unquoted word values. Raises on nothing; unparseable tails
    are returned as-is."""
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


CAT_HEREDOC_RE = re.compile(
    r"^\$\(\s*cat\s+<<-?\s*(['\"]?)([A-Za-z0-9_]+)\1\n(.*)\n\2\s*\)$",
    re.DOTALL,
)

FLAGS_WITH_VALUE = {
    "commit": {"-m": "msg", "--message": "msg", "-F": "file", "--file": "file",
               "-C": "skip", "--reuse-message": "skip", "-c": "skip",
               "--author": "skip", "--date": "skip", "-t": "skip",
               "--template": "skip", "--fixup": "skip", "--squash": "skip",
               "--trailer": "trailer"},
    "pr": {"-t": "title", "--title": "title", "-b": "body", "--body": "body",
           "-F": "bodyfile", "--body-file": "bodyfile", "-B": "skip",
           "--base": "skip", "-H": "skip", "--head": "skip",
           "-a": "skip", "--assignee": "skip", "-l": "skip", "--label": "skip",
           "-m": "skip", "--milestone": "skip", "-p": "skip", "--project": "skip",
           "-r": "skip", "--reviewer": "skip", "-R": "skip", "--repo": "skip",
           "-T": "skip", "--template": "skip"},
    "issue": {"-t": "title", "--title": "title", "-b": "body", "--body": "body",
              "-F": "bodyfile", "--body-file": "bodyfile",
              "-a": "skip", "--assignee": "skip", "-l": "skip", "--label": "skip",
              "-m": "skip", "--milestone": "skip", "-p": "skip", "--project": "skip",
              "-R": "skip", "--repo": "skip", "-T": "skip", "--template": "skip",
              "--add-label": "skip", "--remove-label": "skip",
              "--add-assignee": "skip", "--remove-assignee": "skip",
              "--add-project": "skip", "--remove-project": "skip",
              "--edit-last": "flagonly"},
}


class Artifact:
    def __init__(self, kind):
        self.kind = kind          # commit | pr | issue
        self.title = ""           # PR/issue title (commit: first line = subject)
        self.body = ""
        self.source = "none"      # inline | heredoc | file | none
        self.signoff_flag = False
        self.extraction_failed = False

    @property
    def subject(self):
        if self.kind == "commit":
            return (self.body.split("\n", 1)[0] if self.body else "").strip()
        return self.title.strip()

    @property
    def full(self):
        return (self.title + "\n" + self.body).strip() if self.title else self.body


def extract(seg, heredocs, kind, cwd):
    art = Artifact(kind)
    seg2 = strip_assignments(seg.lstrip())
    try:
        words = tokenize(seg2)
    except Exception:
        art.extraction_failed = True
        return art
    flags = FLAGS_WITH_VALUE[kind]
    msgs = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("-s", "--signoff") and kind == "commit":
            art.signoff_flag = True
            i += 1
            continue
        name, val = w, None
        if w.startswith("--") and "=" in w:
            name, val = w.split("=", 1)
        role = flags.get(name)
        if role in (None, "flagonly"):
            i += 1
            continue
        if val is None:
            if i + 1 >= len(words):
                i += 1
                continue
            val = words[i + 1]
            i += 2
        else:
            i += 1
        if role == "skip" or role == "trailer":
            if role == "trailer":
                msgs.append(val)  # trailers via --trailer count as message text
            continue
        if role == "title":
            art.title = val
            if art.source == "none":
                art.source = "inline"
        elif role == "msg" or role == "body":
            m = CAT_HEREDOC_RE.match(val.strip())
            if m:
                msgs.append(m.group(3))
                # quoted delimiter → no expansion hazards; treat like a file
                art.source = "heredoc" if m.group(1) else "inline"
            else:
                msgs.append(val)
                art.source = "inline"
        elif role == "bodyfile" or role == "file":
            if val == "-":
                if heredocs:
                    msgs.append(heredocs[0])
                    art.source = "heredoc"
                else:
                    art.extraction_failed = True
            else:
                path = os.path.expanduser(val)
                if not os.path.isabs(path):
                    path = os.path.join(cwd or ".", path)
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        msgs.append(f.read())
                    art.source = "file"
                except OSError:
                    art.extraction_failed = True
    art.body = "\n\n".join(msgs)
    if not art.body and not art.title and not art.signoff_flag:
        # editor-based flow or --fill: nothing to validate
        art.extraction_failed = True
    return art


# ------------------------------------------------------------------ rules


FENCE_RE = re.compile(r"^(```|~~~)")


def code_stripped_lines(text):
    """Yield (line, is_code) with fenced blocks marked and inline
    `code` spans blanked out of prose lines."""
    out = []
    in_fence = False
    for line in text.split("\n"):
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            out.append(("", True))
            continue
        if in_fence:
            out.append(("", True))
        else:
            out.append((re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), line), False))
    return out


def prose(text):
    return "\n".join(l for l, is_code in code_stripped_lines(text) if not is_code)


TRAILER_RE = re.compile(
    r"(?im)^(Co-Authored-By|Signed-off-by|Acked-by|Reviewed-by):\s*(.+)$"
)
ATTRIBUTION_RES = [
    re.compile(r"🤖\s*Generated with"),
    re.compile(r"(?i)generated (?:by|with) (?:\[?an? )?\[?claude"),
    re.compile(r"(?i)co-authored-by:.*claude"),
]
CLOSE_KW = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
CLOSING_MULTI_RE = re.compile(
    rf"(?i)\b{CLOSE_KW}\b:?\s+#\d+(?:\s*,\s*(?:and\s+)?#\d+)+"
)
CLOSING_NEG_RE = re.compile(
    rf"(?i)\b(?:not|never|no|doesn'?t|does\s+not|won'?t|isn'?t|without|don'?t)\b"
    rf"\W+(?:\w+\W+){{0,3}}?{CLOSE_KW}\s+#\d+"
)
SUMMARY_RE = re.compile(r"</summary>[ \t]*\n(?![ \t]*\n)(?![ \t]*$)")
CHECKBOX_RE = re.compile(r"(?m)^\s*[-*+] \[ \]")
LOCAL_PATH_RE = re.compile(
    r"""(?:^|[\s("'\[<])((?:/tmp|/Users|/private|/var/folders|~/\.claude)/[^\s)"'\]>,]*)"""
)
STRUCT_TOKEN_RE = re.compile(r"^\s{0,3}(?:[-*+]\s|\d+[.)]\s|>\s?|#{1,6}\s|=+\s*$)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
CI_CMD_RE = re.compile(
    r"(?i)\b(?:(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?(?:test|lint|check|typecheck|build)"
    r"|make\s+(?:test|check|lint)|go\s+(?:test|vet|build)"
    r"|cargo\s+(?:test|check|clippy|build|fmt)|pytest|tox|ruff|mypy|eslint|tsc)\b"
)
MENTION_RE = re.compile(r"(?<![\w.@/`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)\b")
BARE_REF_RE = re.compile(r"(?<![\w&/#])#(\d{1,6})\b")
REF_INTENT_RE = re.compile(
    r"(?i)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|see|refs?|references?"
    r"|issues?|prs?|pull|follow[- ]?up(?:\s+to)?|relate[sd]?(?:\s+to)?|address(?:es|ed)?"
    r"|track(?:s|ed|ing)?|per|via|from|in|of|and|or|,)\W{0,3}$"
)
# require both a digit and a letter: pure digits are dates/numbers, not SHAs
HEX_RE = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


def run_checks(art, cfg):
    """Return list of findings: dicts {tier, rule, msg}."""
    f = []
    add = lambda tier, rule, msg: f.append({"tier": tier, "rule": rule, "msg": msg})
    body_prose = prose(art.body)
    full_prose = prose(art.full)
    kind = art.kind

    # ---- Tier A
    if kind == "commit":
        subj = art.subject
        if len(subj) > 70:
            add("A", "subject-length",
                f"Commit subject is {len(subj)} chars (limit ~70) — shorten it.")
        if subj.endswith(".") and not subj.endswith("..."):
            add("A", "subject-period", "Drop the trailing period from the commit subject.")
    signoff_ok = any(s and s in cfg.get("_cwd", "")
                     for s in (cfg.get("signoff_cwd_substrings") or []))
    for m in TRAILER_RE.finditer(art.full):
        name = m.group(1)
        if name.lower() == "signed-off-by" and signoff_ok:
            continue
        add("A", "trailer",
            f"Remove the `{name}:` trailer — commits/bodies should carry no trailers "
            "unless the repo's contribution guide requires them.")
    if art.signoff_flag and not signoff_ok:
        add("A", "signoff-flag",
            "Drop `-s`/`--signoff` — it adds a Signed-off-by trailer this repo doesn't require.")
    for rex in ATTRIBUTION_RES:
        if rex.search(art.full):
            add("A", "attribution",
                "Remove the Claude/AI attribution line (e.g. `🤖 Generated with ...`, "
                "`Co-Authored-By: Claude ...`).")
            break
    if kind in ("pr", "commit"):
        m = CLOSING_MULTI_RE.search(full_prose)
        if m:
            add("A", "closing-multi",
                f"`{m.group(0)}` only closes the first issue — GitHub needs one closing "
                "keyword per issue: `Closes #1. Closes #2.`")
        m = CLOSING_NEG_RE.search(full_prose)
        if m:
            add("A", "closing-negated",
                f"`{m.group(0).strip()}`: GitHub ignores the negation and will still "
                "auto-close that issue on merge. Say `leaves #N open` / `does not address #N` instead.")
    if kind in ("pr", "issue") and SUMMARY_RE.search(art.body):
        add("A", "summary-blank-line",
            "Add a blank line after `</summary>` — without it the Markdown inside "
            "`<details>` doesn't render.")
    if kind == "pr" and CHECKBOX_RE.search(body_prose):
        add("A", "open-checkbox",
            "PR body contains an open checklist item (`- [ ]`). Do the verification "
            "first and report the outcome — don't leave open checkboxes for the reviewer.")
    if art.source == "inline" and art.body:
        hazards = [ch for ch in ("`", "$", "!") if ch in art.body]
        if len(art.full) > INLINE_BODY_LIMIT or hazards:
            why = (f"contains {', '.join(repr(h) for h in hazards)}" if hazards
                   else f"is {len(art.full)} chars")
            fix = "--file" if kind == "commit" else "--body-file"
            add("A", "inline-body",
                f"Inline message/body {why} — shell quoting will mangle it. Write the "
                f"body to a temp file with the Write tool and pass `{fix} <path>`.")
    for m in LOCAL_PATH_RE.finditer(full_prose):
        add("A", "local-path",
            f"`{m.group(1)}` is a local-only path — the artifact is permanent; remove "
            "references to scratch/local files or replace with a sharable source.")
        break

    # ---- Tier B
    lines = code_stripped_lines(art.body)
    for idx in range(1, len(lines)):
        line, is_code = lines[idx]
        prev, prev_code = lines[idx - 1]
        if is_code or prev_code:
            continue
        if (STRUCT_TOKEN_RE.match(line) and prev.strip()
                and not STRUCT_TOKEN_RE.match(prev)
                and len(prev.strip()) > 30
                and not re.search(r"""[:.!?]["')\]]?\s*$""", prev)):
            add("B", "hard-wrap",
                f"Line {idx + 1} starts with `{line.strip()[:12]}…` mid-paragraph — a "
                "hard-wrapped line landing a Markdown token at column 0 breaks rendering. "
                "Keep each paragraph on one line and let GitHub wrap.")
            break
    if kind == "pr":
        bullet_shas = sum(
            1 for l, c in lines
            if not c and re.match(r"^\s*[-*+]\s", l)
            and (re.search(r"\b[0-9a-f]{7,40}\b", l) or re.match(r"^\s*[-*+]\s+commit\b", l))
        )
        if bullet_shas >= 2:
            add("B", "commit-enumeration",
                "PR body enumerates branch commits — the commit list is already on the "
                "PR and drifts after rebases. Describe the change as a whole.")
        section = None
        section_has_link = False
        section_bullets = []
        sections = []
        for l, c in lines + [("# _end", False)]:
            hm = HEADING_RE.match(l.strip()) if not c else None
            if hm:
                if section is not None:
                    sections.append((section, section_has_link, section_bullets))
                section, section_has_link, section_bullets = hm.group(1), False, []
                continue
            if section is not None and not c:
                if re.search(r"#\d+|https?://", l):
                    section_has_link = True
                if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", l):
                    section_bullets.append(l)
        for title, has_link, bullets in sections:
            if re.match(r"(?i)^(follow[- ]?ups?|next steps|future work)\b", title) and not has_link:
                add("B", "followups-no-links",
                    f"Section “{title}” lists follow-ups with no filed issues — "
                    "conversation-only ideas don't belong on the PR; file issues and link them.")
            if (re.match(r"(?i)^test(ing|\s+plan)?\b", title) and bullets
                    and all(CI_CMD_RE.search(b) for b in bullets)):
                add("B", "testplan-restates-ci",
                    f"Section “{title}” only restates CI commands — pointing at CI is "
                    "enough; list only verification beyond CI, if any.")

    # ---- Tier C
    c_tokens = []
    for m in MENTION_RE.finditer(full_prose):
        c_tokens.append("@" + m.group(1))
    for m in BARE_REF_RE.finditer(full_prose):
        if not REF_INTENT_RE.search(full_prose[max(0, m.start() - 30):m.start()]):
            c_tokens.append("#" + m.group(1))
    for m in HEX_RE.finditer(full_prose):
        c_tokens.append(m.group(0)[:12])
    if c_tokens:
        toks = sorted(set(c_tokens))
        shown = ", ".join(f"`{t}`" for t in toks[:8])
        add("C", "bare-autolink",
            f"Bare token(s) {shown} will ping a user / auto-link on GitHub. Wrap in "
            "backticks if meant literally; if the mention/link is intentional, re-run "
            "the identical command to proceed.")
        f[-1]["tokens"] = toks
    return f


# ------------------------------------------------------------------ state


def state_path(name):
    return os.path.join(STATE_DIR, name)


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def cleanup_state():
    try:
        now = time.time()
        for entry in os.listdir(STATE_DIR):
            p = os.path.join(STATE_DIR, entry)
            try:
                if now - os.path.getmtime(p) > STATE_MAX_AGE:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


def transcript_compact_ts(transcript_path):
    """Timestamp (epoch) of the last compact_boundary in the transcript
    tail, or None."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - TRANSCRIPT_TAIL_BYTES)
            tail = fh.read().decode("utf-8", errors="replace")
        pos = tail.rfind("compact_boundary")
        if pos == -1:
            return None
        line_start = tail.rfind("\n", 0, pos) + 1
        line_end = tail.find("\n", pos)
        line = tail[line_start : line_end if line_end != -1 else len(tail)]
        obj = json.loads(line)
        ts = obj.get("timestamp")
        if not ts:
            return None
        import datetime

        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ------------------------------------------------------------------- main


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


def build_primer(kinds, session_id, transcript_path):
    """Return primer text for categories not yet injected this session
    (respecting the re-arm policy), and write their markers."""
    cats = ["shared"] + [k for k in ("commit", "pr", "issue") if k in kinds]
    now = time.time()
    compact_ts = None
    compact_checked = False
    needed = []
    for cat in cats:
        marker = state_path(f"primer-{session_id}-{cat}")
        try:
            mtime = os.path.getmtime(marker)
            if now - mtime > PRIMER_TTL:
                needed.append(cat)
                continue
            if not compact_checked:
                compact_ts = transcript_compact_ts(transcript_path) if transcript_path else None
                compact_checked = True
            if compact_ts and compact_ts > mtime:
                needed.append(cat)
        except OSError:
            needed.append(cat)
    if not needed:
        return None
    parts = []
    for cat in needed:
        p = os.path.join(PRIMER_DIR, f"primer-{cat}.md")
        try:
            with open(p, encoding="utf-8") as fh:
                parts.append(fh.read().strip())
        except OSError:
            pass
    if not parts:
        return None
    for cat in needed:
        try:
            with open(state_path(f"primer-{session_id}-{cat}"), "w") as fh:
                fh.write(str(int(now)))
        except OSError:
            pass
    return "\n\n".join(parts)


def main():
    data = json.load(sys.stdin)
    if data.get("hook_event_name") != "PreToolUse":
        return
    if data.get("tool_name") != "Bash":
        return
    cmd = (data.get("tool_input") or {}).get("command")
    if not cmd or not isinstance(cmd, str):
        return
    session_id = re.sub(r"[^A-Za-z0-9-]", "_", str(data.get("session_id") or "nosession"))
    cwd = data.get("cwd") or os.getcwd()
    transcript_path = data.get("transcript_path")

    matches = []
    for seg, heredocs in split_top_level(cmd):
        kind, _ = match_kind(seg)
        if kind:
            matches.append((seg, heredocs, kind))
    if not matches:
        return  # not our command: stay silent, normal permission flow applies

    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    cfg["_cwd"] = cwd

    ensure_state_dir()
    kinds = sorted({k for _, _, k in matches})

    findings = []
    for seg, heredocs, kind in matches:
        art = extract(seg, heredocs, kind, cwd)
        if art.extraction_failed and not art.full:
            continue  # nothing to validate: fall through to primer path
        for fi in run_checks(art, cfg):
            fi["kind"] = kind
            findings.append(fi)

    # Tier C ack-and-retry: suppress C findings whose token set was
    # already presented once this session.
    c_findings = [fi for fi in findings if fi["tier"] == "C"]
    if c_findings:
        tokset = sorted({t for fi in c_findings for t in fi.get("tokens", [])})
        key = hashlib.sha256(("\n".join(tokset)).encode()).hexdigest()[:16]
        ack_marker = state_path(f"ack-{session_id}-{key}")
        if os.path.exists(ack_marker):
            findings = [fi for fi in findings if fi["tier"] != "C"]
        else:
            try:
                with open(ack_marker, "w") as fh:
                    fh.write("")
            except OSError:
                pass

    cleanup_state()

    if findings:
        rules = sorted({fi["rule"] for fi in findings})
        guard_path = state_path(f"loop-{session_id}-{'-'.join(kinds)}")
        prev = {"count": 0, "rules": []}
        try:
            with open(guard_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except Exception:
            pass
        shrunk = set(rules) < set(prev.get("rules") or [])
        if prev.get("count", 0) >= 2 and not shrunk:
            # loop guard: fail open with findings as a warning
            try:
                os.unlink(guard_path)
            except OSError:
                pass
            warn = "Hygiene warnings (not blocking): " + " | ".join(
                fi["msg"] for fi in findings
            )
            emit(decision="allow", context=warn)
            return
        try:
            with open(guard_path, "w") as fh:
                json.dump({"count": (prev.get("count", 0) + 1) if not shrunk else 1,
                           "rules": rules}, fh)
        except OSError:
            pass
        lines = [f"Hygiene check ({', '.join(kinds)}) — "
                 f"{len(findings)} finding(s); fix all and re-run:"]
        for i, fi in enumerate(findings, 1):
            lines.append(f"{i}. [{fi['kind']}] {fi['msg']}")
        emit(decision="deny", reason="\n".join(lines))
        return

    # clean pass: clear loop-guard state, allow, maybe attach primer
    try:
        os.unlink(state_path(f"loop-{session_id}-{'-'.join(kinds)}"))
    except OSError:
        pass
    primer = build_primer(kinds, session_id, transcript_path)
    emit(decision="allow", context=primer)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # fail open: never block a Bash call on a dispatcher error
        sys.exit(0)
    sys.exit(0)
