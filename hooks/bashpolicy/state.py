"""On-disk state, config, and session primers.

Paths match `hygiene-dispatch.py` so existing markers and config keep
working across the switch. Both are overridable by environment variable
so the test suite never touches the real `~/.claude` state.
"""

import datetime
import json
import os
import time

BASE = os.environ.get("BASH_POLICY_HOME") or os.path.expanduser("~/.claude/hooks")
STATE_DIR = os.path.join(BASE, ".state")
CONFIG_PATH = os.path.join(BASE, "hygiene-config.json")

PRIMER_DIR = os.environ.get("BASH_POLICY_PRIMER_DIR") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hygiene")
)

PRIMER_TTL = 4 * 3600
STATE_MAX_AGE = 7 * 24 * 3600
TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024


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
        ts = json.loads(line).get("timestamp")
        if not ts:
            return None
        return datetime.datetime.fromisoformat(
            ts.replace("Z", "+00:00")
        ).timestamp()
    except Exception:
        return None


def build_primer(cats, session_id, transcript_path):
    """Primer text for categories not yet injected this session, writing
    their markers. Re-arms when the marker ages past PRIMER_TTL or the
    transcript gained a newer compact boundary."""
    now = time.time()
    compact_ts = None
    compact_checked = False
    needed = []
    for cat in cats:
        mtime = read_marker(f"primer-{session_id}-{cat}")
        if mtime is None:
            needed.append(cat)
            continue
        if now - mtime > PRIMER_TTL:
            needed.append(cat)
            continue
        if not compact_checked:
            compact_ts = (transcript_compact_ts(transcript_path)
                          if transcript_path else None)
            compact_checked = True
        if compact_ts and compact_ts > mtime:
            needed.append(cat)
    if not needed:
        return None
    parts = []
    for cat in needed:
        try:
            with open(os.path.join(PRIMER_DIR, f"primer-{cat}.md"),
                      encoding="utf-8") as fh:
                parts.append(fh.read().strip())
        except OSError:
            pass
    if not parts:
        return None
    for cat in needed:
        write_marker(f"primer-{session_id}-{cat}", str(int(now)))
    return "\n\n".join(parts)
