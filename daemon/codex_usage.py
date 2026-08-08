"""Read Codex CLI rate-limit usage from local session rollout files.

Unlike the Claude side (which spends a probe API call to read quota out of the
response headers), Codex needs no network access at all: the CLI already writes
a rate-limit snapshot into its session rollout after every turn. We just read
the newest one back.

    ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl

Each rollout is JSONL; the turn-accounting events carry:

    "rate_limits": {
        "limit_id": "codex",
        "primary":   {"used_percent": 14.0, "window_minutes": 300,   "resets_at": 1782966954},
        "secondary": {"used_percent": 2.0,  "window_minutes": 10080, "resets_at": 1783553754},
        "plan_type": "plus"
    }

``primary`` is the 5-hour window (300 min) and ``secondary`` the weekly one
(10080 min) — structurally identical to Anthropic's unified-5h / unified-7d
pair, so both providers collapse onto the same wire payload.

Three things make this harder than "read the last line":

1. Rollouts get large — hundreds of MB is normal once tool output is inlined —
   so the file is scanned backwards from the end, never parsed whole.
2. The newest rollout by mtime may hold no rate_limits at all (a session that
   errored before its first turn completed), so several are tried in order.
3. A snapshot is only true until its window resets. Past ``resets_at`` the
   window has rolled and the recorded percentage is simply wrong — reporting a
   stale 14% would be worse than reporting the 0% the window almost certainly
   holds now.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Cap the backward scan per file. A rate_limits event lands at the end of every
# turn, so the last one is near the tail; anything beyond this is a rollout
# whose final turn inlined a huge tool result, and the next file is a better bet
# than reading megabytes to reach it.
MAX_TAIL_BYTES = 4 * 1024 * 1024
CHUNK = 256 * 1024

# How many recent rollouts to try before giving up.
MAX_FILES = 8

# window_minutes → which slot the window belongs in. Codex reports the actual
# window length, so match on it rather than trusting primary/secondary ordering.
FIVE_HOUR_MIN = 300
WEEKLY_MIN = 10080

# Fixed English abbreviations rather than strftime("%a"): %a follows the host
# locale, and a zh_CN Mac would emit "周四" — glyphs the device's subsetted
# Styrene fonts don't carry, so the label would render blank.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def weekday_abbrev(epoch: float) -> str:
    """Local-time weekday abbreviation for an absolute epoch, or "" if unusable.

    Local time is the right frame: the user wants to know which of *their* days
    the window rolls over on.
    """
    try:
        return WEEKDAYS[time.localtime(epoch).tm_wday]
    except (ValueError, OSError, OverflowError):
        return ""


def codex_home() -> Path:
    """Root of the Codex CLI's state, honoring CODEX_HOME like the CLI does."""
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _find_key(obj, key: str):
    """First value for ``key`` anywhere in a nested JSON structure.

    The rate_limits object sits inside an event envelope whose shape has moved
    between Codex releases, so search for the key instead of hardcoding a path.
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def _rate_limits_from_tail(path: Path) -> dict | None:
    """Newest rate_limits object in one rollout, scanning backwards from EOF."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            buf = b""
            pos = size
            while pos > 0 and len(buf) < MAX_TAIL_BYTES:
                step = min(CHUNK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf

                # Only whole lines can be parsed. The first fragment is dropped
                # unless we reached the start of the file, where it is complete.
                lines = buf.split(b"\n")
                if pos > 0:
                    lines = lines[1:]

                for line in reversed(lines):
                    if b'"rate_limits"' not in line:
                        continue
                    try:
                        rl = _find_key(json.loads(line), "rate_limits")
                    except (ValueError, UnicodeDecodeError):
                        continue  # partial or malformed line; keep scanning
                    if isinstance(rl, dict):
                        return rl
    except OSError:
        return None
    return None


def _window(w, now: float) -> tuple[int, int] | None:
    """(used_percent, minutes_until_reset) for one window, or None if unusable.

    Past its reset the window has rolled over, so the recorded percentage no
    longer describes the current window — report the 0% it reset to.
    """
    if not isinstance(w, dict):
        return None
    pct = w.get("used_percent")
    if not isinstance(pct, (int, float)):
        return None

    resets_at = w.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        # Older builds report a relative countdown instead of an absolute stamp.
        # There is no record of when it was taken, so anchoring it to now is the
        # best available reading — it over-states the remaining time by however
        # long ago the snapshot was written.
        secs = w.get("resets_in_seconds")
        if not isinstance(secs, (int, float)):
            return int(round(pct)), 0
        resets_at = now + secs

    remaining = (resets_at - now) / 60.0
    if remaining <= 0:
        return 0, 0
    return int(round(pct)), int(round(remaining))


def _reset_epoch(w, now: float) -> float | None:
    """Absolute reset timestamp for one window, or None when unusable.

    Mirrors _window's resolution (absolute stamp preferred, relative countdown
    anchored to now), but lives apart so _window keeps returning a 2-tuple —
    callers and tests unpack it positionally.
    """
    if not isinstance(w, dict):
        return None
    resets_at = w.get("resets_at")
    if isinstance(resets_at, (int, float)):
        return float(resets_at)
    secs = w.get("resets_in_seconds")
    if isinstance(secs, (int, float)):
        return now + secs
    return None


def read_codex_usage(home: Path | None = None) -> dict | None:
    """Codex usage as wire-payload fields, or None when unavailable.

    Returns ``{"xs", "xsr", "xw", "xwr", "xacct"}`` — the Codex mirror of the
    Claude ``s``/``sr``/``w``/``wr``/``acct`` keys. None means "no Codex data":
    the CLI isn't installed, has never run, or its rollouts carry no rate
    limits. Callers omit the keys entirely in that case so the device falls back
    to the single-provider view.
    """
    root = home or codex_home()
    sessions = root / "sessions"
    if not sessions.is_dir():
        return None

    now = time.time()
    for path in _recent_rollouts(sessions):
        rl = _rate_limits_from_tail(path)
        if not rl:
            continue
        out = _map_windows(rl, now)
        if out:
            return out
    return None


def _map_windows(rl: dict, now: float) -> dict | None:
    """Map one rate_limits object onto payload fields, or None if unusable.

    How many windows Codex reports, and which slot each lands in, varies by plan
    and CLI version. A ChatGPT Plus account on Codex 0.147 exposes a single
    weekly limit sitting in ``primary`` with ``secondary`` null; other setups
    report a 5-hour window in ``primary`` and the weekly one in ``secondary``.
    So slots carry no meaning — windows are identified by ``window_minutes``,
    and one window is a perfectly valid answer.

    The headline (``xs``) is the shortest window present, since that is the one
    that bites first, with ``xwin`` naming its length so the display can label
    it. A second window rides along in ``xw`` only when there actually is one.
    """
    ordered: list[tuple[object, tuple[int, int]]] = []
    by_len: dict[int, tuple[int, int]] = {}
    raw_by_len: dict[int, dict] = {}
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        parsed = _window(w, now)
        if parsed is None:
            continue  # absent, null, or malformed — simply not reported
        length = w.get("window_minutes")
        ordered.append((length, parsed))
        if isinstance(length, int):
            by_len[length] = parsed
            raw_by_len[length] = w

    if not ordered:
        return None

    five, week = by_len.get(FIVE_HOUR_MIN), by_len.get(WEEKLY_MIN)
    if five is not None:
        headline_len, headline, secondary = FIVE_HOUR_MIN, five, week
    elif week is not None:
        headline_len, headline, secondary = WEEKLY_MIN, week, None
    else:
        # Window lengths we don't recognize: fall back to the order reported.
        headline_len, headline = ordered[0]
        secondary = ordered[1][1] if len(ordered) > 1 else None

    out = {"xs": headline[0], "xsr": headline[1]}
    if isinstance(headline_len, int):
        out["xwin"] = headline_len
    if secondary is not None:
        out["xw"], out["xwr"] = secondary
    # Which weekday the weekly window rolls over on — the thing a countdown of
    # "6d 23h" doesn't actually tell you. Attached to the weekly window whether
    # it is the secondary one or (on plans reporting only a weekly limit) the
    # headline itself. Omitted past the stamp: the snapshot's window has already
    # rolled, so its weekday describes a reset that has been and gone.
    weekly_raw = raw_by_len.get(WEEKLY_MIN)
    if weekly_raw is not None:
        epoch = _reset_epoch(weekly_raw, now)
        if epoch is not None and epoch > now:
            day = weekday_abbrev(epoch)
            if day:
                out["xwd"] = day
    plan = rl.get("plan_type")
    if isinstance(plan, str) and plan:
        out["xacct"] = plan
    return out


def _recent_rollouts(sessions: Path) -> list[Path]:
    """Up to MAX_FILES rollout paths, newest first.

    Descends the YYYY/MM/DD partitioning newest-branch-first rather than walking
    the whole tree — a long-lived ~/.codex holds thousands of rollouts, and only
    the newest few can carry a current snapshot.
    """
    found: list[Path] = []

    def _sorted_dirs(p: Path) -> list[Path]:
        try:
            return sorted((d for d in p.iterdir() if d.is_dir()),
                          key=lambda d: d.name, reverse=True)
        except OSError:
            return []

    for year in _sorted_dirs(sessions):
        for month in _sorted_dirs(year):
            for day in _sorted_dirs(month):
                try:
                    files = [f for f in day.iterdir()
                             if f.name.startswith("rollout-") and f.suffix == ".jsonl"]
                except OSError:
                    continue
                files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                found.extend(files)
                if len(found) >= MAX_FILES:
                    return found[:MAX_FILES]
    return found[:MAX_FILES]
