#!/usr/bin/env python3
"""Unit tests for the weekly-reset weekday ("wd") both Claude daemons emit.

A "6d 23h" countdown doesn't say which day the weekly window rolls over on,
which is what you actually need to plan around it. The daemons derive the day
from the 7d reset header and ship it alongside the countdown.

Parametrized over the macOS and Windows daemons deliberately: the two files are
kept deliberately parallel, and this is the cheapest place for a drift between
them to show up.

Run: python -m pytest daemon/tests/test_reset_weekday.py -x -q
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import daemon.claude_usage_daemon as macos
import daemon.claude_usage_daemon_windows as windows
from daemon.codex_usage import WEEKDAYS, weekday_abbrev

DAEMONS = [pytest.param(macos, id="macos"), pytest.param(windows, id="windows")]


def _run(coro):
    """Fresh loop per call — see the note in test_windows_poll.py."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _poll(mod, headers):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "mocked"
    resp.headers = MagicMock()
    resp.headers.get = lambda name, default=None: headers.get(name.lower(), default)

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)

    with patch("httpx.AsyncClient", return_value=client):
        return _run(mod.poll_api("fake-token"))


def _pro_headers(reset_7d, reset_5h=None):
    now = time.time()
    return {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-5h-reset": str(reset_5h if reset_5h is not None
                                                    else now + 3600),
        "anthropic-ratelimit-unified-7d-utilization": "0.10",
        "anthropic-ratelimit-unified-7d-reset": str(reset_7d),
        "anthropic-ratelimit-unified-5h-status": "allowed",
    }


@pytest.mark.parametrize("mod", DAEMONS)
def test_pro_payload_carries_weekly_reset_weekday(mod):
    reset_7d = time.time() + 4 * 86400
    payload = _poll(mod, _pro_headers(reset_7d))
    assert payload["wd"] == weekday_abbrev(reset_7d)
    assert payload["wd"] in WEEKDAYS


@pytest.mark.parametrize("mod", DAEMONS)
def test_weekday_is_ascii(mod):
    """Guards the locale trap: strftime("%a") on a zh_CN host emits "周四",
    which the device's subsetted fonts render as blanks."""
    payload = _poll(mod, _pro_headers(time.time() + 4 * 86400))
    assert payload["wd"].isascii()


@pytest.mark.parametrize("mod", DAEMONS)
def test_unparseable_reset_stamp_omits_weekday(mod):
    """Absent beats wrong: the firmware falls back to the bare countdown."""
    payload = _poll(mod, _pro_headers("not-a-number"))
    assert "wd" not in payload


@pytest.mark.parametrize("mod", DAEMONS)
def test_past_reset_stamp_omits_weekday(mod):
    """A stamp already behind us names a reset that has been and gone."""
    payload = _poll(mod, _pro_headers(time.time() - 600))
    assert "wd" not in payload


@pytest.mark.parametrize("mod", DAEMONS)
def test_enterprise_payload_has_no_weekday(mod):
    """Enterprise has no weekly window — it reports a monthly spending period,
    already labelled with a date ("rd")."""
    now = time.time()
    headers = {
        "anthropic-ratelimit-unified-overage-utilization": "0.30",
        "anthropic-ratelimit-unified-overage-reset": str(now + 10 * 86400),
        "anthropic-ratelimit-unified-status": "allowed",
    }
    payload = _poll(mod, headers)
    assert payload["acct"] == "ent"
    assert "wd" not in payload
