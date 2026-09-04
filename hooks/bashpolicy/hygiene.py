"""Commit-message / PR / issue hygiene rules.

Carried over from `hygiene-dispatch.py` with the rules unchanged. What
changed is the plumbing: the artifact is extracted from the already
tokenized `Invocation` instead of re-parsing the segment, and the
findings go through the shared fold.

These rules are ADVISORY: a bug here must never block work, and the
existing loop guard and ack-and-retry escapes are preserved.
"""

import hashlib
import os
import re

from . import state
from .policy import ADVISORY, Finding, rule

HYGIENE_COMMANDS = [
    (("git", "commit"), "commit"),
    (("gh", "pr", "create"), "pr"),
    (("gh", "pr", "edit"), "pr"),
    (("gh", "pr", "comment"), "pr"),
    (("gh", "issue", "create"), "issue"),
    (("gh", "issue", "comment"), "issue"),
    (("gh", "issue", "edit"), "issue"),
]

INLINE_BODY_LIMIT = 200

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


def kind_of(inv):
    for prefix, kind in HYGIENE_COMMANDS:
        if inv.matches(prefix):
            return kind
    return None


class Artifact:
    def __init__(self, kind):
        self.kind = kind          # commit | pr | issue
        self.title = ""           # PR/issue title (commit: subject = first line)
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


def extract(inv, kind, cwd):
    """Build the Artifact from an already-tokenized invocation."""
    art = Artifact(kind)
    words = inv.words
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
                if inv.heredocs:
                    msgs.append(inv.heredocs[0])
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
            out.append(
                (re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), line), False)
            )
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


def run_checks(art, cfg, cwd):
    """Return list of findings: dicts {tier, rule, msg}."""
    f = []
    add = lambda tier, name, msg: f.append({"tier": tier, "rule": name, "msg": msg})
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
            add("A", "subject-period",
                "Drop the trailing period from the commit subject.")
    signoff_ok = any(s and s in (cwd or "")
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
            "Drop `-s`/`--signoff` — it adds a Signed-off-by trailer this repo "
            "doesn't require.")
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
                "auto-close that issue on merge. Say `leaves #N open` / "
                "`does not address #N` instead.")
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
                "hard-wrapped line landing a Markdown token at column 0 breaks "
                "rendering. Keep each paragraph on one line and let GitHub wrap.")
            break
    if kind == "pr":
        bullet_shas = sum(
            1 for l, c in lines
            if not c and re.match(r"^\s*[-*+]\s", l)
            and (re.search(r"\b[0-9a-f]{7,40}\b", l)
                 or re.match(r"^\s*[-*+]\s+commit\b", l))
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
            if (re.match(r"(?i)^(follow[- ]?ups?|next steps|future work)\b", title)
                    and not has_link):
                add("B", "followups-no-links",
                    f"Section “{title}” lists follow-ups with no filed issues — "
                    "conversation-only ideas don't belong on the PR; file issues "
                    "and link them.")
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


@rule("hygiene", ADVISORY, [prefix for prefix, _ in HYGIENE_COMMANDS])
def check_hygiene(invocations, ctx):
    """Validate every drafted commit / PR / issue in the command.

    Receives the whole batch because the Tier C ack-and-retry and the
    loop guard are keyed on the combined finding set, exactly as the
    standalone dispatcher did.
    """
    cfg = ctx.config
    state.ensure_state_dir()

    pairs = []
    for inv in invocations:
        kind = kind_of(inv)
        if kind:
            pairs.append((inv, kind))
    kinds = sorted({k for _, k in pairs})

    findings = []
    for inv, kind in pairs:
        art = extract(inv, kind, ctx.cwd)
        if art.extraction_failed and not art.full:
            continue  # nothing to validate: fall through to the primer path
        for fi in run_checks(art, cfg, ctx.cwd):
            fi["kind"] = kind
            findings.append(fi)

    # Tier C ack-and-retry: suppress C findings whose token set was
    # already presented once this session.
    c_findings = [fi for fi in findings if fi["tier"] == "C"]
    if c_findings:
        tokset = sorted({t for fi in c_findings for t in fi.get("tokens", [])})
        key = hashlib.sha256(("\n".join(tokset)).encode()).hexdigest()[:16]
        ack = f"ack-{ctx.session_id}-{key}"
        if state.read_marker(ack) is not None:
            findings = [fi for fi in findings if fi["tier"] != "C"]
        else:
            state.write_marker(ack)

    state.cleanup()
    loop_key = f"loop-{ctx.session_id}-{'-'.join(kinds)}"

    if findings:
        rules = sorted({fi["rule"] for fi in findings})
        prev = state.read_json_marker(loop_key, {"count": 0, "rules": []})
        shrunk = set(rules) < set(prev.get("rules") or [])
        if prev.get("count", 0) >= 2 and not shrunk:
            # loop guard: fail open with the findings as a warning
            state.drop_marker(loop_key)
            warn = "Hygiene warnings (not blocking): " + " | ".join(
                fi["msg"] for fi in findings
            )
            return [Finding("allow", "hygiene", context=warn)]
        state.write_json_marker(loop_key, {
            "count": (prev.get("count", 0) + 1) if not shrunk else 1,
            "rules": rules,
        })
        return [
            Finding("deny", f"{fi['kind']}/{fi['rule']}", msg=fi["msg"],
                    tier=ADVISORY)
            for fi in findings
        ]

    # Clean pass: clear loop-guard state and assert allow, with the
    # primer attached if this session still needs it. The allow is
    # deliberate — it is what keeps a validated `git commit` from
    # falling through to a permission prompt — so it is returned even
    # when there is no primer to carry.
    state.drop_marker(loop_key)
    cats = ["shared"] + [k for k in ("commit", "pr", "issue") if k in kinds]
    primer = state.build_primer(cats, ctx.session_id, ctx.transcript_path)
    return [Finding("allow", "hygiene", context=primer)]
