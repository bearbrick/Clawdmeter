#!/usr/bin/env python3
"""Unit tests for the Codex rollout reader.

Covers the tail scan, window mapping, staleness rollover, and the
degrade-to-None paths that keep the device on its single-provider view.

Run: python -m pytest daemon/tests/test_codex_usage.py -x -q
"""
import json
import time

from daemon.codex_usage import _rate_limits_from_tail, _window, read_codex_usage


def _rate_limits(primary_pct=14.0, secondary_pct=2.0, offset_5h=3600,
                 offset_week=600000, plan="plus"):
    now = time.time()
    return {
        "limit_id": "codex",
        "limit_name": None,
        "primary": {"used_percent": primary_pct, "window_minutes": 300,
                    "resets_at": now + offset_5h},
        "secondary": {"used_percent": secondary_pct, "window_minutes": 10080,
                      "resets_at": now + offset_week},
        "credits": None,
        "plan_type": plan,
    }


def _write_rollout(root, day="2026/08/07", name="rollout-a.jsonl",
                   events=None, filler_lines=0, filler_len=0):
    """Build a rollout JSONL under root/sessions/<day>/ and return its path."""
    d = root / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    with path.open("w", encoding="utf-8") as f:
        for _ in range(filler_lines):
            f.write(json.dumps({"type": "message", "text": "x" * filler_len}) + "\n")
        for ev in events or []:
            f.write(json.dumps(ev) + "\n")
    return path


def _event(rl):
    """A rate_limits event wrapped the way Codex nests it."""
    return {"timestamp": "2026-08-07T12:00:00Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {}, "rate_limits": rl}}


# ---------------------------------------------------------------------------
# tail scan
# ---------------------------------------------------------------------------

def test_finds_rate_limits_in_last_line(tmp_path):
    rl = _rate_limits()
    p = _write_rollout(tmp_path, events=[_event(rl)])
    assert _rate_limits_from_tail(p)["plan_type"] == "plus"


def test_returns_newest_when_several_present(tmp_path):
    old = _rate_limits(primary_pct=5.0)
    new = _rate_limits(primary_pct=41.0)
    p = _write_rollout(tmp_path, events=[_event(old), {"type": "noise"}, _event(new)])
    assert _rate_limits_from_tail(p)["primary"]["used_percent"] == 41.0


def test_scans_back_past_large_trailing_lines(tmp_path):
    """The snapshot is reached even when later lines push it out of one chunk."""
    rl = _rate_limits(primary_pct=33.0)
    p = _write_rollout(tmp_path, events=[_event(rl)] + [{"type": "big", "d": "y" * 50_000}
                                                        for _ in range(20)])
    assert _rate_limits_from_tail(p)["primary"]["used_percent"] == 33.0


def test_missing_file_returns_none(tmp_path):
    assert _rate_limits_from_tail(tmp_path / "nope.jsonl") is None


def test_malformed_line_is_skipped(tmp_path):
    """A truncated line mentioning rate_limits must not mask the real one."""
    rl = _rate_limits(primary_pct=7.0)
    p = _write_rollout(tmp_path, events=[_event(rl)])
    with p.open("a", encoding="utf-8") as f:
        f.write('{"payload": {"rate_limits": {"primary": \n')
    assert _rate_limits_from_tail(p)["primary"]["used_percent"] == 7.0


# ---------------------------------------------------------------------------
# window mapping
# ---------------------------------------------------------------------------

def test_window_converts_reset_to_minutes():
    now = time.time()
    pct, mins = _window({"used_percent": 14.0, "resets_at": now + 3600}, now)
    assert (pct, mins) == (14, 60)


def test_window_past_reset_reports_zero():
    """A rolled-over window's recorded percentage no longer applies."""
    now = time.time()
    assert _window({"used_percent": 88.0, "resets_at": now - 10}, now) == (0, 0)


def test_window_accepts_relative_countdown():
    now = time.time()
    pct, mins = _window({"used_percent": 20.0, "resets_in_seconds": 1800}, now)
    assert (pct, mins) == (20, 30)


def test_window_rejects_non_numeric_percent():
    assert _window({"used_percent": None, "resets_at": time.time() + 60}, time.time()) is None


# ---------------------------------------------------------------------------
# end-to-end payload
# ---------------------------------------------------------------------------

def test_read_maps_windows_by_length(tmp_path):
    _write_rollout(tmp_path, events=[_event(_rate_limits(14.0, 2.0))])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 14 and out["xw"] == 2
    assert out["xsr"] == 60          # 3600s
    assert out["xwr"] == 10000       # 600000s
    assert out["xacct"] == "plus"


def test_read_matches_by_window_minutes_not_order(tmp_path):
    """Slots are chosen by window length, so a swapped payload still maps right."""
    rl = _rate_limits(14.0, 2.0)
    rl["primary"], rl["secondary"] = rl["secondary"], rl["primary"]
    _write_rollout(tmp_path, events=[_event(rl)])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 14 and out["xw"] == 2


def test_read_falls_back_to_positional_order(tmp_path):
    """Unrecognized window lengths fall back to primary=5h, secondary=weekly."""
    rl = _rate_limits(9.0, 3.0)
    rl["primary"].pop("window_minutes")
    rl["secondary"].pop("window_minutes")
    _write_rollout(tmp_path, events=[_event(rl)])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 9 and out["xw"] == 3


def test_read_skips_rollouts_without_snapshots(tmp_path):
    """A newer but snapshot-free rollout must not shadow an older good one."""
    _write_rollout(tmp_path, day="2026/08/06", name="rollout-old.jsonl",
                   events=[_event(_rate_limits(21.0))])
    _write_rollout(tmp_path, day="2026/08/07", name="rollout-new.jsonl",
                   events=[{"type": "message", "text": "no snapshot here"}])
    assert read_codex_usage(tmp_path)["xs"] == 21


def test_read_prefers_newest_day(tmp_path):
    _write_rollout(tmp_path, day="2026/08/06", name="rollout-old.jsonl",
                   events=[_event(_rate_limits(21.0))])
    _write_rollout(tmp_path, day="2026/08/07", name="rollout-new.jsonl",
                   events=[_event(_rate_limits(55.0))])
    assert read_codex_usage(tmp_path)["xs"] == 55


def test_weekly_only_plan_is_the_headline(tmp_path):
    """Codex 0.147 on ChatGPT Plus: one weekly window in `primary`, secondary null.

    The weekly window has to become the headline number — treating a missing 5h
    window as "no data" is what made this shape read as None.
    """
    rl = _rate_limits()
    rl["primary"] = {"used_percent": 15.0, "window_minutes": 10080,
                     "resets_at": time.time() + 600000}
    rl["secondary"] = None
    _write_rollout(tmp_path, events=[_event(rl)])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 15
    assert out["xwin"] == 10080        # display labels it "Week resets"
    assert "xw" not in out             # no second window to fold in
    assert out["xacct"] == "plus"


def test_five_hour_only_plan_is_the_headline(tmp_path):
    rl = _rate_limits()
    rl["secondary"] = None
    _write_rollout(tmp_path, events=[_event(rl)])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 14 and out["xwin"] == 300
    assert "xw" not in out


def test_five_hour_wins_headline_when_both_present(tmp_path):
    """The shortest window bites first, so it gets the big number."""
    _write_rollout(tmp_path, events=[_event(_rate_limits(14.0, 2.0))])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 14 and out["xwin"] == 300
    assert out["xw"] == 2


def test_both_windows_null_returns_none(tmp_path):
    rl = _rate_limits()
    rl["primary"] = rl["secondary"] = None
    _write_rollout(tmp_path, events=[_event(rl)])
    assert read_codex_usage(tmp_path) is None


def test_read_without_codex_returns_none(tmp_path):
    """No ~/.codex at all — caller omits the keys and the device stays single-provider."""
    assert read_codex_usage(tmp_path) is None


def test_read_with_empty_sessions_returns_none(tmp_path):
    (tmp_path / "sessions").mkdir()
    assert read_codex_usage(tmp_path) is None


def test_stale_snapshot_reports_zero_not_stale_numbers(tmp_path):
    """The month-old-rollout case: windows have rolled, so report 0, not 68%."""
    _write_rollout(tmp_path, events=[_event(
        _rate_limits(13.0, 68.0, offset_5h=-100000, offset_week=-50000))])
    out = read_codex_usage(tmp_path)
    assert out["xs"] == 0 and out["xw"] == 0
    assert out["xsr"] == 0 and out["xwr"] == 0
