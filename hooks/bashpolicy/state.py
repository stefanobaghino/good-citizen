"""On-disk state, config, and per-context primers.

The state directory and the primer directory are both overridable by
environment variable, so the test suite never touches the real
`~/.claude` state.
"""

import json
import os
import time

BASE = os.environ.get("BASH_POLICY_HOME") or os.path.expanduser("~/.claude/hooks")
STATE_DIR = os.path.join(BASE, ".state")
CONFIG_PATH = os.path.join(BASE, "hygiene-config.json")

PRIMER_DIR = os.environ.get("BASH_POLICY_PRIMER_DIR") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hygiene")
)

# A primer stops working because it has fallen behind in the context
# window, not because time has passed. `primer_token_step` overrides.
PRIMER_TOKEN_STEP = 200_000
PRIMER_CATS = ("shared", "commit", "pr", "issue", "comments")
STATE_MAX_AGE = 7 * 24 * 3600
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024

USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")

# "unreadable" is environmental; "drift" means the transcript no longer
# looks the way this module expects and the code needs updating.
NOTICES = {
    "unreadable": (
        "Primer re-arm unavailable: this session's transcript could not be "
        "read, so the hygiene primer will not be re-injected as the "
        "conversation grows. The commit itself is unaffected. Mention this "
        "to the user rather than investigating it yourself."
    ),
    "drift": (
        "Primer re-arm is disabled: the transcript's `usage` fields are no "
        "longer in the shape hooks/bashpolicy/state.py expects, so context "
        "growth cannot be measured and the primer will arrive only once per "
        "session. This needs a code fix. Report it to the user rather than "
        "stopping to fix it yourself."
    ),
}


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def state_path(name):
    return os.path.join(STATE_DIR, name)


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def cleanup():
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


def read_marker(name):
    try:
        return os.path.getmtime(state_path(name))
    except OSError:
        return None


def write_marker(name, content=""):
    try:
        with open(state_path(name), "w") as fh:
            fh.write(content)
        return True
    except OSError:
        return False


def drop_marker(name):
    try:
        os.unlink(state_path(name))
    except OSError:
        pass


def read_json_marker(name, default):
    try:
        with open(state_path(name), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_marker(name, obj):
    try:
        with open(state_path(name), "w") as fh:
            json.dump(obj, fh)
    except OSError:
        pass


def transcript_context_tokens(transcript_path):
    """`(tokens, problem)` for the newest assistant turn in the transcript
    tail — the size of the context that turn was answered with.

    `problem` is None on success, "unreadable" when there is no transcript
    to read, and "drift" when assistant turns are there but none carries a
    usable `usage`, which today can only mean the format changed. Lines are
    scanned individually and bad ones skipped, so a line truncated by the
    tail seek costs that line rather than the whole signal.
    """
    if not transcript_path:
        return None, "unreadable"
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > TRANSCRIPT_TAIL_BYTES:
                fh.seek(size - TRANSCRIPT_TAIL_BYTES)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None, "unreadable"

    saw_assistant = False
    for line in reversed(tail.splitlines()):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        saw_assistant = True
        usage = (obj.get("message") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        # Synthetic and refusal-fallback turns carry a usage block that
        # sums to zero; they are not a reading of the context.
        total = sum(usage.get(f) or 0 for f in USAGE_FIELDS)
        if total:
            return total, None
    return None, ("drift" if saw_assistant else "unreadable")


def token_step():
    try:
        step = int(load_config().get("primer_token_step") or 0)
    except (AttributeError, TypeError, ValueError):
        return PRIMER_TOKEN_STEP
    return step if step > 0 else PRIMER_TOKEN_STEP


def read_token_marker(name):
    """`(present, count)` for a primer marker.

    `present` is False when the marker is absent or predates this scheme
    (it then holds a bare epoch, and re-arming once is the right recovery).
    `count` is None when the marker records an injection whose context size
    could not be read — injected, but with no baseline to measure from.
    """
    try:
        with open(state_path(name), encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return False, None
    if not raw.startswith("tok:"):
        return False, None
    body = raw[4:]
    if body == "?":
        return True, None
    try:
        return True, int(body)
    except ValueError:
        return False, None


def write_token_marker(name, count):
    write_marker(name, "tok:%s" % ("?" if count is None else count))


def notice_for(session_id, who, problem):
    """The one-off warning for `problem`, or None if this context has
    already been told."""
    name = f"notice-{session_id}-{who}-{problem}"
    if read_marker(name) is not None:
        return None
    write_marker(name)
    return NOTICES.get(problem)


def primer_marker(session_id, agent_id, cat):
    return f"primer-{session_id}-{agent_id or 'main'}-{cat}"


def read_primer(cat):
    try:
        with open(os.path.join(PRIMER_DIR, f"primer-{cat}.md"),
                  encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def start_primers(session_id, agent_id, baseline):
    """Every primer, for a context that is just starting, with the markers
    written at `baseline` so the per-command hook only re-sends them once
    the context has grown a step past it."""
    ensure_state_dir()
    parts = [p for p in (read_primer(cat) for cat in PRIMER_CATS) if p]
    for cat in PRIMER_CATS:
        write_token_marker(primer_marker(session_id, agent_id, cat), baseline)
    return "\n\n".join(parts) or None


def build_primer(cats, session_id, agent_id, transcript_path):
    """`(primer text, notice)` for the categories this context still needs,
    writing their markers.

    The session-start hook normally writes every marker first, so this
    only re-sends; a missing marker (the hook not registered, or a context
    that predates it) still gets the primer on its first watched command.

    A category is due when it has no marker, when the context has grown a
    step since its last injection, or when the count has *dropped* — which
    only happens on a compaction, the point at which the primer is gone
    from the context entirely.

    `agent_id` is present only inside a subagent, and it is part of the
    marker name because the main thread and each subagent hold separate
    contexts: without it the first one to run a watched command consumes
    the primer for all of them.
    """
    who = agent_id or "main"
    names = {cat: primer_marker(session_id, agent_id, cat) for cat in cats}
    marks = {cat: read_token_marker(names[cat]) for cat in cats}

    cur, problem = transcript_context_tokens(transcript_path)
    step = token_step()

    needed = []
    for cat in cats:
        present, prev = marks[cat]
        if not present:
            needed.append(cat)
        elif prev is None or cur is None:
            continue  # no baseline to measure against
        elif cur < prev or cur - prev >= step:
            needed.append(cat)

    # A first injection needs no count, so it loses nothing and has
    # nothing to report.
    notice = None
    if problem and any(present for present, _ in marks.values()):
        notice = notice_for(session_id, who, problem)

    if not needed:
        return None, notice

    parts = [p for p in (read_primer(cat) for cat in needed) if p]
    if not parts:
        return None, notice

    for cat in needed:
        write_token_marker(names[cat], cur)
    return "\n\n".join(parts), notice


def join_context(*parts):
    return "\n\n".join(p for p in parts if p) or None
