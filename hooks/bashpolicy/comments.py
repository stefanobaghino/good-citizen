"""Comment guidance on commits whose diff adds comments (ADVISORY).

A trigger, not a judge: it reports that comments are present and hands
over the guide, and never blocks. Proxies for a *bad* comment (length,
phrasing, punctuation density) were tried and dropped — they miss
inaccuracy and odd register entirely, and the shapes they do catch
include the rationale and correctness arguments that are worth writing.

The full guide arrives once per session (`state.build_primer` also
re-arms it after a compaction, which is when it is most needed); later
commits get a one-line reminder naming what is in that diff, plus the
path to the guide so the wording stays recoverable.
"""

import os
import re

from . import state
from .githist import git
from .policy import ADVISORY, Finding, rule

# `//` plus `/* ... */`; every other mapped suffix is `#`-commented.
SLASH_STAR = (".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".groovy",
              ".gradle", ".kts", ".scala", ".c", ".h", ".cpp", ".go",
              ".rs", ".swift", ".css", ".scss")
HASH = (".pro", ".py", ".yaml", ".yml", ".sh", ".bash", ".zsh", ".rb",
        ".toml", ".cfg", ".conf", ".tf", ".pl")

# Machine-written trees: their comments are not the author's to answer for.
SKIP_SEGMENTS = frozenset(("generated", "node_modules", "build", "vendor",
                           "dist", "target", "out"))

# Bundled shorts are not split by the parser, so `-am` has to match here
# as well as `-a`. The single leading dash keeps `--amend` out.
STAGES_TRACKED = re.compile(r"-[a-zA-Z]*a[a-zA-Z]*$")

MAX_NAMED_FILES = 3


def _skipped(path):
    return any(seg in SKIP_SEGMENTS for seg in path.split("/"))


def _diff_args(inv):
    """`git commit -a` stages at commit time, so the index is still
    empty when the hook runs and `--cached` would see nothing."""
    for tok in inv.opts(1):
        if tok == "--all" or (not tok.startswith("--")
                              and STAGES_TRACKED.match(tok)):
            return ["diff", "HEAD", "-U0"]
    return ["diff", "--cached", "-U0"]


def _blocks_by_file(diff):
    """Count runs of added comment lines, keyed by file.

    Runs reset on hunk headers as well as file headers: two hunks print
    back to back while being far apart in the file.
    """
    counts = {}
    path = None
    run = 0
    inblock = False

    def close():
        nonlocal run
        if run:
            counts[path] = counts.get(path, 0) + 1
            run = 0

    for line in diff.splitlines():
        if line.startswith("+++ "):
            close()
            raw = line[4:].strip()
            path = None if raw == "/dev/null" else raw[2:]
            inblock = False
            continue
        if line.startswith("@@"):
            close()
            inblock = False
            continue
        if path is None or _skipped(path):
            continue
        if not line.startswith("+"):
            close()
            inblock = False
            continue

        body = line[1:].strip()
        ext = os.path.splitext(path)[1].lower()
        if ext in SLASH_STAR:
            if inblock:
                comment = True
                inblock = not body.endswith("*/")
            elif body.startswith(("/*", "/**")):
                comment = True
                inblock = not body.endswith("*/")
            else:
                comment = body.startswith("//")
        elif ext in HASH:
            comment = body.startswith("#") and not body.startswith("#!")
        else:
            comment = False

        if comment:
            run += 1
        else:
            close()
    close()
    return counts


def _reminder(counts):
    total = sum(counts.values())
    names = sorted(counts, key=lambda p: (-counts[p], p))[:MAX_NAMED_FILES]
    shown = ", ".join(f"`{os.path.basename(p)}`" for p in names)
    if len(counts) > len(names):
        shown += f" and {len(counts) - len(names)} more"
    guide = os.path.join(state.PRIMER_DIR, "primer-comments.md")
    plural = "s" if total != 1 else ""
    return (f"This commit adds {total} comment block{plural} in {shown} — "
            f"apply the recovery, staleness and subject tests before "
            f"committing. Full guide: {guide}")


@rule("comments", ADVISORY, [("git", "commit")])
def check_comments(invocations, ctx):
    inv = invocations[0]
    proc = git(ctx, [*inv.gitopts, *_diff_args(inv)])
    if proc.returncode != 0 or not proc.stdout:
        return []

    counts = _blocks_by_file(proc.stdout)
    if not counts:
        return []

    state.ensure_state_dir()
    primer = state.build_primer(["comments"], ctx.session_id,
                                ctx.transcript_path)
    return [Finding("allow", "comments",
                    context=primer or _reminder(counts))]
