"""/cotas: saved quota snapshots, limit detection and the Telegram command.

No agent is run: Claude events are decoded from recorded shapes and Codex
rollouts are written to a temporary CODEX_HOME.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import threading
import time

import anyio
import pytest

import takopi.quota_status as quota_status
import takopi.telegram.commands.quota as quota_command
from takopi.markdown import MarkdownPresenter
from takopi.model import CompletedEvent, ResumeToken, StartedEvent, TakopiEvent
from takopi.quota_status import (
    AVAILABLE,
    NO_DATA,
    QUOTA_ENGINES,
    QUOTA_GROUPS,
    UNAVAILABLE,
    QuotaStore,
    QuotaView,
    detect_usage_limit,
    render_quota_report,
    resolve_quota_path,
)
from takopi.router import AutoRouter, RunnerEntry
from takopi.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from takopi.runners.agy_telemetry import apply_reports
from takopi.runners.claude import ClaudeStreamState, translate_claude_event
from takopi.runners.codex_telemetry import latest_codex_quota
from takopi.runners.mock import Emit, Return, ScriptRunner, Wait
from takopi.schemas.claude import decode_stream_json_line
from takopi.telegram.commands.menu import build_bot_commands
from takopi.telegram.loop import run_main_loop
from takopi.telegram.types import TelegramIncomingMessage
from takopi.telemetry import Quota, Telemetry, quota_line, telemetry_event, timestamp
from takopi.transport_runtime import TransportRuntime
from tests.telegram_fakes import FakeTransport, _empty_projects, make_cfg

NOW = 1_800_000_000.0
HOUR = 3600
DAY = 86400


def _ok() -> CompletedEvent:
    return CompletedEvent(engine="claude", ok=True, answer="feito")


def _error(text: str) -> CompletedEvent:
    return CompletedEvent(engine="claude", ok=False, answer="", error=text)


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def test_store_persists_reloads_and_skips_identical_writes(tmp_path, monkeypatch):
    path = resolve_quota_path(tmp_path / "takopi.toml")
    assert path == tmp_path / "quota_status_state.json"
    store = QuotaStore(path)
    status = Telemetry(
        model="opus", five_hour=Quota(42, NOW + HOUR), weekly=Quota(61, NOW + DAY)
    )
    store.observe("claude", status, _error("Usage limit reached · session sid-123"))
    store.observe("claude", status, None, now=NOW)
    saved = path.read_text()
    writes = []
    monkeypatch.setattr(
        quota_status, "atomic_write_json", lambda *args: writes.append(args)
    )
    store.observe("claude", status, None, now=NOW)
    store.observe("codex", Telemetry(), _ok(), now=NOW)  # nothing to record
    store.observe("opencode", status, None, now=NOW)  # not shown by /cotas
    assert writes == []
    assert QuotaStore(path).view("claude", NOW) == store.view("claude", NOW)
    # Only percentages, times and the limit flag reach the file.
    data = json.loads(saved)
    assert set(data) == {"version", "engines"} and set(data["engines"]) == {"claude"}
    assert set(data["engines"]["claude"][""]) == {
        "five_hour",
        "weekly",
        "observed_at",
        "limited",
        "limited_at",
        "limited_until",
    }
    assert set(data["engines"]["claude"][""]["weekly"]) == {
        "used_percent",
        "resets_at",
        "observed_at",
    }
    assert "sid-123" not in saved and "Usage" not in saved and "opus" not in saved


@pytest.mark.parametrize(
    "content", [b"{not json", b'{"version": 2, "engines": {}}', b'{"engines": 1}']
)
def test_unreadable_state_starts_empty(tmp_path, content):
    path = tmp_path / "quota_status_state.json"
    path.write_bytes(content)
    store = QuotaStore(path)
    assert store.status_for("claude", NOW).label == NO_DATA
    store.observe("claude", Telemetry(five_hour=Quota(5, NOW + 60)), None, now=NOW)
    assert QuotaStore(path).status_for("claude", NOW).label == AVAILABLE


def test_missing_values_keep_previous_and_agy_groups_stay_apart(tmp_path):
    store = QuotaStore(tmp_path / "quota.json")
    store.observe(
        "claude",
        Telemetry(five_hour=Quota(10, NOW + 60), weekly=Quota(20, NOW + DAY)),
        None,
        now=NOW - 60,
    )
    store.observe("claude", Telemetry(five_hour=Quota(30, NOW + 60)), None, now=NOW)
    (claude,) = store.view("claude", NOW).groups
    assert claude.five_hour == Quota(30, NOW + 60)
    assert claude.weekly == Quota(20, NOW + DAY) and claude.observed_at == NOW

    def agy(group, five_hour, weekly=None):
        return Telemetry(five_hour=five_hour, weekly=weekly, quota_group=group)

    store.observe("agy", agy("Gemini", Quota(20, NOW + 60)), None, now=NOW)
    store.observe("agy", agy("Claude/GPT", Quota(100, NOW + 60)), None, now=NOW)
    store.observe("agy", agy(None, Quota(99, NOW + 60)), None, now=NOW)  # unknown
    groups = {group.label: group.five_hour for group in store.view("agy", NOW).groups}
    assert groups == {"Gemini": Quota(20, NOW + 60), "Claude/GPT": Quota(100, NOW + 60)}
    partial = store.status_for("agy", NOW)
    assert partial.label == AVAILABLE and partial.exhausted == ("Claude/GPT",)
    report = render_quota_report([store.view("agy", NOW)], NOW)
    assert "DISPONÍVEL (parcial: Claude/GPT esgotado)" in report
    store.observe(
        "agy", agy("Gemini", None, Quota(100, NOW + DAY)), None, now=NOW
    )  # every known family exhausted: back when the first one renews
    assert store.status_for("agy", NOW).label == UNAVAILABLE
    assert store.status_for("agy", NOW).until == NOW + 60
    assert store.status_for("agy", NOW + 61).exhausted == ("Gemini",)


@pytest.mark.parametrize(
    ("status", "observed_at", "expected", "shown"),
    [
        (Telemetry(five_hour=Quota(100, NOW + 2880)), NOW, UNAVAILABLE, "~48min"),
        (Telemetry(five_hour=Quota(100, NOW - 1)), NOW - 60, AVAILABLE, "renovada"),
        (Telemetry(five_hour=Quota(100)), NOW - 4 * HOUR, UNAVAILABLE, "~1h 0min"),
        (Telemetry(five_hour=Quota(100)), NOW - 6 * HOUR, AVAILABLE, "renovada"),
        (Telemetry(weekly=Quota(100)), NOW - 6 * DAY, UNAVAILABLE, "~1d 0h"),
        (Telemetry(weekly=Quota(100)), NOW - 8 * DAY, AVAILABLE, "renovada"),
        (Telemetry(five_hour=Quota(99, NOW + 60)), NOW, AVAILABLE, "99% usado"),
    ],
    ids=[
        "full-future-reset",
        "full-reset-passed",
        "full-no-reset-inside-5h",
        "full-no-reset-after-5h",
        "weekly-no-reset-inside-7d",
        "weekly-no-reset-after-7d",
        "99-percent",
    ],
)
def test_status_follows_windows_and_their_renewal(
    tmp_path, status, observed_at, expected, shown
):
    store = QuotaStore(tmp_path / "quota.json")
    store.observe("codex", status, None, now=observed_at)
    assert store.status_for("codex", NOW).label == expected
    report = render_quota_report([store.view("codex", NOW)], NOW)
    assert f"**Codex** — {expected}" in report and shown in report
    if shown == "renovada":
        assert "renovada (aguardando atualização)" in report
        assert "100% usado" not in report


def test_no_data_and_unconfigured_engines(tmp_path):
    store = QuotaStore(tmp_path / "quota.json")
    assert store.status_for("claude", NOW).label == NO_DATA
    report = render_quota_report(
        [
            store.view("claude", NOW),
            QuotaView("codex", note="CLI ausente"),
            QuotaView("agy", note="não configurado"),
        ],
        NOW,
    )
    assert "**Claude** — SEM DADOS" in report
    assert "**Codex** — CLI ausente" in report and "**Agy** — não configurado" in report
    assert "%" not in report.split("\n\n")[1]


def test_limit_always_expires_without_a_later_successful_run(tmp_path):
    path = tmp_path / "quota.json"
    store = QuotaStore(path)
    store.observe("claude", Telemetry(), _error("Usage limit reached"), now=NOW)
    assert json.loads(path.read_text())["engines"]["claude"][""]["limited_until"]
    status = store.status_for("claude", NOW)
    assert status.label == UNAVAILABLE and status.until == NOW + 5 * HOUR
    assert "volta em ~5h 0min" in render_quota_report([store.view("claude", NOW)], NOW)
    later = NOW + 5 * HOUR + 1
    assert QuotaStore(path).status_for("claude", later).label == AVAILABLE

    def until(five_hour, weekly):
        store = QuotaStore(tmp_path / f"{five_hour}-{weekly}.json")
        windows = Telemetry(
            five_hour=Quota(five_hour, NOW + HOUR), weekly=Quota(weekly, NOW + 3 * DAY)
        )
        store.observe("codex", windows, _error("You've hit your limit"), now=NOW)
        assert store.status_for("codex", NOW).label == UNAVAILABLE
        return store.status_for("codex", NOW).until

    # An exhausted window holds until its reset, even if another is nearly full.
    assert until(100, 93) == NOW + HOUR
    assert until(95, 97) == NOW + 3 * DAY  # Latest of the nearly full windows.
    assert until(40, 50) == NOW + HOUR  # Earliest known reset.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Usage limit reached", True),
        ("Claude AI usage limit reached|1793232000", True),
        ("5-hour limit reached ∙ resets 3pm", True),
        ("You've hit your limit · resets 3pm (America/Sao_Paulo)", True),
        ("You've hit your usage limit. Try again in 2 hours.", True),
        ("You've hit your 5-hour limit · resets 3:40pm (America/Sao_Paulo)", True),
        ("You've hit your weekly limit", True),
        ('{"code": "usage_limit_reached"}', True),
        ("Limite de uso atingido", True),
        ("Context limit reached · /compact or /clear to continue", False),
        ("You've hit your context limit", False),
        ("Rate limit 429 retry", False),
        ("network error", False),
        ("claude failed (rc=1).", False),
        (None, False),
    ],
)
def test_detect_usage_limit_is_conservative(text, expected):
    assert detect_usage_limit(text) is expected


def test_results_and_verdicts_set_or_clear_limits(tmp_path):
    store = QuotaStore(tmp_path / "quota.json")
    windows = Telemetry(five_hour=Quota(80, NOW + HOUR), weekly=Quota(50, NOW + DAY))
    store.observe("codex", windows, _error("Usage limit reached"), now=NOW)
    store.observe("codex", Telemetry(), _error("network error"), now=NOW + 1)
    store.observe("codex", Telemetry(), _error("429 Too Many Requests"), now=NOW + 2)
    assert store.status_for("codex", NOW + 3).label == UNAVAILABLE
    store.observe("codex", Telemetry(), _ok(), now=NOW + 4)
    assert store.status_for("codex", NOW + 5).label == AVAILABLE
    store.observe("claude", replace(windows, limited=True), None, now=NOW)
    assert store.status_for("claude", NOW).label == UNAVAILABLE
    store.observe("claude", Telemetry(limited=False), None, now=NOW + 1)
    assert store.status_for("claude", NOW + 1).label == AVAILABLE
    store.observe(
        "agy",
        replace(windows, quota_group="Gemini"),
        _error("Usage limit reached"),
        now=NOW,
    )
    assert store.status_for("agy", NOW).label == AVAILABLE


def test_refreshing_one_window_does_not_extend_another_without_reset(tmp_path):
    path = tmp_path / "quota.json"
    store = QuotaStore(path)
    store.observe("claude", Telemetry(weekly=Quota(100)), None, now=NOW)
    store.observe(
        "claude",
        Telemetry(five_hour=Quota(20, NOW + 8 * DAY)),
        None,
        now=NOW + 6 * DAY,
    )
    store = QuotaStore(path)
    assert store.status_for("claude", NOW + 6 * DAY).until == NOW + 7 * DAY
    assert store.status_for("claude", NOW + 7 * DAY + 1).label == AVAILABLE
    report = render_quota_report(
        [store.view("claude", NOW + 7 * DAY + 1)], NOW + 7 * DAY + 1
    )
    assert "Semanal: renovada (aguardando atualização)" in report


def test_legacy_window_keeps_its_original_observation_time(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "engines": {
                    "claude": {
                        "": {"observed_at": NOW, "weekly": {"used_percent": 100}}
                    }
                },
            }
        )
    )
    store = QuotaStore(path)
    store.observe("claude", Telemetry(five_hour=Quota(20)), None, now=NOW + 6 * DAY)
    assert QuotaStore(path).status_for("claude", NOW + 6 * DAY).until == NOW + 7 * DAY


def test_older_outside_data_does_not_undo_a_recorded_limit(tmp_path):
    store = QuotaStore(tmp_path / "quota.json")
    store.observe("codex", Telemetry(), _error("Usage limit reached"), now=NOW)
    fresh = Telemetry(five_hour=Quota(1, NOW + HOUR), limited=False)
    assert not store.merge_if_newer("codex", fresh, NOW - 10)
    assert store.status_for("codex", NOW).label == UNAVAILABLE
    assert store.merge_if_newer("codex", fresh, NOW + 10)
    assert store.status_for("codex", NOW + 11).label == AVAILABLE


def test_agy_is_out_only_when_every_known_family_is_exhausted(tmp_path):
    # The labels must stay the ones the Agy reader produces.
    labels = {
        apply_reports(
            Telemetry(),
            {"id": model},
            {"groups": [{"buckets": [{"id": f"{prefix}5h", "remaining_fraction": 0}]}]},
        ).quota_group
        for model, prefix in (("gemini-flash", "gemini-"), ("claude-sonnet", "3p-"))
    }
    assert labels == set(QUOTA_GROUPS["agy"])
    store = QuotaStore(tmp_path / "quota.json")
    exhausted = Telemetry(five_hour=Quota(100, NOW + HOUR), quota_group="Claude/GPT")
    store.observe("agy", exhausted, None, now=NOW)
    # Gemini has no data yet and may still work.
    status = store.status_for("agy", NOW)
    assert status.label == AVAILABLE and status.exhausted == ("Claude/GPT",)
    report = render_quota_report([store.view("agy", NOW)], NOW)
    assert "DISPONÍVEL (parcial: Claude/GPT esgotado)" in report
    store.observe("agy", replace(exhausted, quota_group="Gemini"), None, now=NOW)
    assert store.status_for("agy", NOW).label == UNAVAILABLE


@pytest.mark.parametrize("huge", ["1e999", "9" * 400])
def test_hand_edited_state_drops_invalid_windows_and_clamps_usage(tmp_path, huge):
    path = tmp_path / "quota_status_state.json"
    nan, inf = float("nan"), float("inf")
    state = {
        "version": 1,
        "engines": {
            "claude": {
                "": {
                    "five_hour": {"used_percent": "HUGE"},
                    "weekly": {"used_percent": -5, "resets_at": NOW + DAY},
                    "observed_at": NOW - 60,
                }
            },
            "codex": {
                "": {
                    "five_hour": {"used_percent": nan},
                    "weekly": {"used_percent": 150, "resets_at": NOW + DAY},
                    "observed_at": inf,
                    "limited": True,
                    "limited_at": NOW - 60,
                    "limited_until": nan,
                }
            },
            "agy": {
                "Gemini": {
                    "five_hour": "full",
                    "weekly": {"used_percent": 50, "resets_at": -inf},
                    "observed_at": NOW - 60,
                }
            },
        },
    }
    path.write_text(json.dumps(state).replace('"HUGE"', huge))
    assert all(value in path.read_text() for value in (huge, "NaN", "-Infinity"))
    store = QuotaStore(path)
    (claude,) = store.view("claude", NOW).groups
    assert claude.five_hour is None and claude.weekly == Quota(0, NOW + DAY)
    (codex,) = store.view("codex", NOW).groups
    assert codex.five_hour is None and codex.weekly == Quota(100, NOW + DAY)
    assert codex.observed_at is None
    assert store.status_for("claude", NOW).label == AVAILABLE
    # Only the clamped window blocks Codex: the NaN limit time was dropped.
    assert store.status_for("codex", NOW).until == NOW + DAY
    assert store.status_for("agy", NOW).label == NO_DATA
    views = [store.view(engine, NOW) for engine in QUOTA_ENGINES]
    report = render_quota_report(views, NOW)
    assert "Semanal: 0% usado" in report and "Semanal: 100% usado" in report


def test_failed_write_is_retried_and_never_raises(tmp_path, monkeypatch):
    path = tmp_path / "quota.json"
    store = QuotaStore(path)
    write = quota_status.atomic_write_json
    attempts = []
    failing = [True]

    def flaky(target, payload):
        attempts.append(target)
        if failing[0]:
            raise OSError("disk full")
        write(target, payload)

    monkeypatch.setattr(quota_status, "atomic_write_json", flaky)
    status = Telemetry(five_hour=Quota(42, NOW + HOUR))
    store.observe("claude", status, None, now=NOW)
    assert len(attempts) == 1 and not path.exists()
    assert store.status_for("claude", NOW).label == AVAILABLE
    failing[0] = False
    store.observe("claude", status, None, now=NOW)  # Same data, still unsaved.
    store.observe("claude", status, None, now=NOW)  # Saved: nothing to write.
    assert len(attempts) == 2 and QuotaStore(path).view("claude", NOW).groups
    failing[0] = True
    store.observe("claude", Telemetry(five_hour=Quota(50, NOW + HOUR)), None, now=NOW)
    failing[0] = False
    # Older outside data is refused, but the pending change is saved.
    assert not store.merge_if_newer("claude", status, NOW - DAY)
    (saved,) = QuotaStore(path).view("claude", NOW).groups
    assert len(attempts) == 4 and saved.five_hour == Quota(50, NOW + HOUR)


def test_successful_run_drops_exhausted_windows_it_did_not_report(tmp_path):
    store = QuotaStore(tmp_path / "quota.json")
    full = Telemetry(five_hour=Quota(100, NOW + HOUR), weekly=Quota(60, NOW + DAY))
    store.observe("claude", full, None, now=NOW - 60)
    assert store.status_for("claude", NOW).label == UNAVAILABLE
    store.observe("claude", Telemetry(weekly=Quota(61, NOW + DAY)), _ok(), now=NOW)
    (group,) = store.view("claude", NOW).groups
    assert group.five_hour is None and group.weekly == Quota(61, NOW + DAY)
    assert store.status_for("claude", NOW).label == AVAILABLE
    # A window the same successful run reports as exhausted stays.
    store.observe("claude", Telemetry(weekly=Quota(100, NOW + DAY)), _ok(), now=NOW)
    assert store.status_for("claude", NOW).label == UNAVAILABLE


def _rate_limit(info):
    return decode_stream_json_line(
        json.dumps(
            {"type": "rate_limit_event", "session_id": "sid", "rate_limit_info": info}
        )
    )


@pytest.mark.parametrize(
    ("top", "window", "expected"),
    [
        ("rejected", "allowed", True),
        ("allowed_warning", "rejected", True),
        ("allowed", "allowed", False),
        ("allowed_warning", None, False),
        (None, None, None),
    ],
)
def test_claude_verdict_is_read_apart_from_utilization(top, window, expected):
    state = ClaudeStreamState()
    five_hour: dict[str, object] = {"utilization": 1.02, "resetsAt": 1793206800}
    if window is not None:
        five_hour["status"] = window
    info: dict[str, object] = {
        "unifiedWindows": {"five_hour": five_hour},
        "overageStatus": "rejected",
    }
    if top is not None:
        info["status"] = top
    translate_claude_event(
        _rate_limit(info), title="Claude", state=state, factory=state.factory
    )
    assert state.telemetry.limited is expected
    # Above 100% there is no percentage, unless the window itself is rejected.
    assert state.telemetry.five_hour == (
        Quota(100, 1793206800) if window == "rejected" else None
    )


@pytest.mark.parametrize(
    "info",
    [
        {
            "status": "rejected",
            "unifiedWindows": {
                "five_hour": {
                    "utilization": 1.2,
                    "resetsAt": NOW + 2 * HOUR,
                    "status": "rejected",
                }
            },
        },
        {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "utilization": 1.2,
            "resetsAt": NOW + 2 * HOUR,
        },
    ],
    ids=["unified-windows", "single-window"],
)
def test_rejected_window_above_100_returns_at_its_own_reset(tmp_path, info):
    store = QuotaStore(tmp_path / "quota.json")
    earlier = Telemetry(
        five_hour=Quota(40, NOW + HOUR), weekly=Quota(93, NOW + 3 * DAY)
    )
    store.observe("claude", earlier, _ok(), now=NOW - HOUR)
    state = ClaudeStreamState()
    translate_claude_event(
        _rate_limit(info), title="Claude", state=state, factory=state.factory
    )
    assert state.telemetry.five_hour == Quota(100, NOW + 2 * HOUR)
    store.observe("claude", state.telemetry, _error("You've hit your limit"), now=NOW)
    status = store.status_for("claude", NOW)
    # Not the nearly full weekly window three days away.
    assert status.label == UNAVAILABLE and status.until == NOW + 2 * HOUR


def test_claude_recorded_event_is_allowed_despite_overage_rejection():
    # Shape logged on this machine (ids removed): overage is not the quota.
    info = {
        "status": "allowed_warning",
        "resetsAt": 1793232000,
        "rateLimitType": "seven_day",
        "utilization": 0.93,
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
        "surpassedThreshold": 0.75,
        "unifiedWindows": {
            "five_hour": {
                "utilization": 0.43,
                "resetsAt": 1793206800,
                "status": "allowed",
            },
            "seven_day": {
                "utilization": 0.93,
                "resetsAt": 1793232000,
                "status": "allowed_warning",
                "surpassedThreshold": 0.75,
            },
        },
        "overageEnabled": False,
    }
    state = ClaudeStreamState()
    translate_claude_event(
        _rate_limit(info), title="Claude", state=state, factory=state.factory
    )
    weekly = state.telemetry.weekly
    assert state.telemetry.limited is False and weekly is not None
    assert weekly.used_percent == pytest.approx(93) and weekly.resets_at == 1793232000


def _token_count(when, limits, **extra):
    payload = {"type": "token_count", "info": None, "rate_limits": limits, **extra}
    return json.dumps({"timestamp": when, "type": "event_msg", "payload": payload})


CODEX = {
    "limit_id": "codex",
    "limit_name": None,
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1793526875},
    "secondary": {
        "used_percent": 76.0,
        "window_minutes": 10080,
        "resets_at": 1793793883,
    },
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "plan_type": "plus",
    "rate_limit_reached_type": None,
}
PREMIUM = {
    "limit_id": "premium",
    "primary": None,
    "secondary": None,
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "plan_type": "plus",
    "rate_limit_reached_type": None,
}


def _rollout(home, name, *lines, end="\n"):
    path = home / "sessions" / "2026" / "11" / "01" / f"rollout-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + end)
    return path


def test_latest_codex_quota_reads_windows_behind_premium_records(tmp_path):
    _rollout(
        tmp_path,
        "a",
        _token_count("2026-11-01T04:54:51.577Z", CODEX),
        "not json",
        _token_count("2026-11-01T04:55:00Z", PREMIUM),
        _token_count("2026-11-01T04:56:00Z", None),
        # Still being written: valid JSON, but no newline yet.
        _token_count("2026-11-01T05:00:00Z", {**CODEX, "rate_limit_reached_type": 1}),
        end="",
    )
    found = latest_codex_quota(tmp_path)
    assert found is not None
    observed_at, status = found
    assert observed_at == timestamp("2026-11-01T04:54:51.577Z")
    assert status == Telemetry(
        five_hour=Quota(12, 1793526875), weekly=Quota(76, 1793793883), limited=False
    )


def test_latest_codex_quota_prefers_the_newest_record_over_file_mtime(tmp_path):
    reached = {**CODEX, "rate_limit_reached_type": "primary"}  # unverified value
    newer = _rollout(tmp_path, "b", _token_count("2026-11-01T06:00:00Z", reached))
    _rollout(tmp_path, "c", _token_count("2026-11-01T05:00:00Z", CODEX))
    os.utime(newer, (1, 1))
    found = latest_codex_quota(tmp_path)
    assert found is not None
    assert found[0] == timestamp("2026-11-01T06:00:00Z") and found[1].limited is True


def test_latest_codex_quota_discards_the_record_cut_by_the_tail_read(tmp_path):
    # The tail starts exactly where a valid record sits inside a longer line,
    # which must not be read as if it were whole.
    premium = _token_count("2026-11-01T05:00:00Z", PREMIUM)
    size = 1024 * 1024 - len(premium) - 2  # The tail also holds two newlines.
    cut = _token_count("2026-11-01T06:00:00Z", CODEX, pad="")
    cut = _token_count("2026-11-01T06:00:00Z", CODEX, pad="y" * (size - len(cut)))
    path = _rollout(tmp_path, "d", "x" * 10 + cut, premium)
    assert len(cut) == size and path.stat().st_size == 10 + 1024 * 1024
    assert latest_codex_quota(tmp_path) is None


def test_latest_codex_quota_without_usable_rollouts(tmp_path):
    assert latest_codex_quota(tmp_path / "missing") is None
    _rollout(tmp_path, "e", "[]", "{}", _token_count("bad time", CODEX))
    _rollout(tmp_path, "f", _token_count("2026-11-01T05:00:00Z", PREMIUM))
    assert latest_codex_quota(tmp_path) is None


def test_latest_codex_quota_reads_only_the_codex_limit(tmp_path):
    foreign = {
        **CODEX,
        "limit_id": "codex_other",
        "primary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": 1},
        "rate_limit_reached_type": 1,
    }
    _rollout(
        tmp_path,
        "h",
        _token_count("2026-11-01T04:00:00Z", CODEX),
        _token_count("2026-11-01T05:00:00Z", foreign),
    )
    found = latest_codex_quota(tmp_path)
    assert found is not None and found[0] == timestamp("2026-11-01T04:00:00Z")
    assert found[1].five_hour == Quota(12, 1793526875) and found[1].limited is False
    unnamed = {key: value for key, value in CODEX.items() if key != "limit_id"}
    _rollout(tmp_path, "i", _token_count("2026-11-01T06:00:00Z", unnamed))
    found = latest_codex_quota(tmp_path)
    assert found is not None and found[0] == timestamp("2026-11-01T06:00:00Z")


def test_latest_codex_quota_skips_rollouts_that_vanish(tmp_path, monkeypatch):
    path = _rollout(tmp_path, "j", _token_count("2026-11-01T05:00:00Z", CODEX))
    (path.parent / "rollout-broken.jsonl").symlink_to(path.parent / "missing.jsonl")
    listed = Path.glob

    def glob_with_deleted(self, pattern):
        # As if a file was deleted between the listing and its stat.
        return [*listed(self, pattern), path.parent / "rollout-deleted.jsonl"]

    monkeypatch.setattr(Path, "glob", glob_with_deleted)
    found = latest_codex_quota(tmp_path)
    assert found is not None and found[0] == timestamp("2026-11-01T05:00:00Z")


class _EventsRunner(ScriptRunner):
    # Ends on the given events instead of ScriptRunner's closing success.
    def __init__(self, events: list[TakopiEvent], engine: str = "claude") -> None:
        super().__init__([], engine=engine, resume_value="sid")
        self._final_events = events

    async def run(
        self, prompt: str, resume: ResumeToken | None
    ) -> AsyncIterator[TakopiEvent]:
        for event in self._final_events:
            yield event


def _limit_events() -> list[TakopiEvent]:
    token = ResumeToken("claude", "sid")
    windows = Telemetry(
        five_hour=Quota(100, time.time() + HOUR), weekly=Quota(93, time.time() + DAY)
    )
    return [
        StartedEvent(engine="claude", resume=token, title="Claude"),
        telemetry_event("claude", windows),
        CompletedEvent(
            engine="claude",
            ok=False,
            answer="",
            resume=token,
            error="Usage limit reached",
        ),
    ]


async def _final_text(store, events) -> str:
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
        quota_store=store,
    )
    await handle_message(
        cfg,
        runner=_EventsRunner(events),
        incoming=IncomingMessage(channel_id=1, message_id=1, text="go"),
        resume_token=None,
        clock=lambda: 0.0,
    )
    return transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_bridge_records_windows_and_usage_limit(tmp_path, monkeypatch):
    store = QuotaStore(tmp_path / "quota.json")
    with_store = await _final_text(store, _limit_events())
    status = store.status_for("claude", time.time())
    assert status.label == UNAVAILABLE
    assert status.until == pytest.approx(time.time() + HOUR, abs=5)
    (group,) = store.view("claude", time.time()).groups
    assert group.weekly is not None and group.weekly.used_percent == 93
    assert "Usage limit" not in (tmp_path / "quota.json").read_text()
    # Without a store, or with a failing one, the run is delivered the same way.
    assert await _final_text(None, _limit_events()) == with_store

    def broken(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "observe", broken)
    assert await _final_text(store, _limit_events()) == with_store


@pytest.mark.anyio
async def test_cancelled_run_records_windows_but_not_its_result(tmp_path):
    store = QuotaStore(tmp_path / "quota.json")
    store.observe("claude", Telemetry(), _error("Usage limit reached"))
    hold = anyio.Event()
    windows = Telemetry(five_hour=Quota(55, time.time() + HOUR))
    # A success seen before the cancel is not the run's result.
    runner = ScriptRunner(
        [Emit(telemetry_event("claude", windows)), Emit(_ok()), Wait(hold)],
        engine="claude",
        resume_value="sid",
    )
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport,
        presenter=MarkdownPresenter(),
        final_notify=True,
        quota_store=store,
    )
    running_tasks: dict = {}

    async def run() -> None:
        await handle_message(
            cfg,
            runner=runner,
            incoming=IncomingMessage(channel_id=1, message_id=1, text="go"),
            resume_token=None,
            running_tasks=running_tasks,
        )

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            while not any(
                "5h: 55% usado" in call["message"].text for call in transport.edit_calls
            ):
                await anyio.sleep(0.01)
            (task,) = running_tasks.values()
            task.cancel_requested.set()
    (saved,) = store.view("claude", time.time()).groups
    assert saved.five_hour is not None and saved.five_hour.used_percent == 55
    assert store.status_for("claude", time.time()).label == UNAVAILABLE


def _quota_config(tmp_path, monkeypatch, engines, missing=()):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    entries = [
        RunnerEntry(
            engine=engine,
            runner=ScriptRunner([Return(answer="unexpected")], engine=engine),
            status="missing_cli" if engine in missing else "ok",
        )
        for engine in engines
    ]
    runtime = TransportRuntime(
        router=AutoRouter(entries=entries, default_engine=engines[0]),
        projects=_empty_projects(),
        config_path=tmp_path / "takopi.toml",
    )
    transport = FakeTransport()
    base = make_cfg(transport)
    store = QuotaStore(resolve_quota_path(tmp_path / "takopi.toml"))
    cfg = replace(
        base,
        runtime=runtime,
        allowed_user_ids=(42,),
        exec_cfg=replace(base.exec_cfg, quota_store=store),
    )
    return cfg, transport, store, entries, codex_home


async def _send(cfg, text, sender=42):
    async def poller(_cfg):
        yield TelegramIncomingMessage(
            transport="telegram",
            chat_id=cfg.chat_id,
            message_id=4,
            text=text,
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=sender,
            chat_type="private",
        )

    await run_main_loop(cfg, poller)


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["/cotas", "/quota", "/COTAS@bot"])
@pytest.mark.parametrize("sender", [42, 999])
async def test_cotas_replies_once_without_running_an_agent(
    tmp_path, monkeypatch, command, sender
):
    cfg, transport, store, entries, codex_home = _quota_config(
        tmp_path, monkeypatch, ("codex", "claude", "agy")
    )
    now = time.time()
    limits = {
        **CODEX,
        "primary": {
            "used_percent": 17.0,
            "window_minutes": 300,
            "resets_at": now + HOUR,
        },
        "secondary": {
            "used_percent": 30.0,
            "window_minutes": 10080,
            "resets_at": now + 2 * DAY,
        },
    }
    _rollout(codex_home, "g", _token_count(_iso(now - 120), limits))
    store.observe(
        "claude",
        Telemetry(five_hour=Quota(42, now + HOUR), weekly=Quota(61, now + 3 * DAY)),
        _ok(),
        now=now - 690,
    )
    store.observe(
        "agy",
        Telemetry(five_hour=Quota(20, now + HOUR), quota_group="Gemini"),
        None,
        now=now - 2 * DAY,
    )
    await _send(cfg, command, sender)
    assert not any(entry.runner.calls for entry in entries)
    replies = [
        call
        for call in transport.send_calls
        if call["options"] and call["options"].reply_to
    ]
    if sender != 42:
        assert replies == []
        return
    (reply,) = replies
    text = reply["message"].text
    assert reply["options"].reply_to.message_id == 4
    assert text.index("Claude") < text.index("Codex") < text.index("Agy")
    assert text.count("DISPONÍVEL") == 3 and "INDISPONÍVEL" not in text
    assert "5h: 42% usado" in text and "dado de há 12min" in text
    assert "5h: 17% usado" in text  # Codex, read from its local rollout.
    assert "Gemini · 5h: 20% usado" in text and "pode estar desatualizado" in text
    assert "plus" not in text and "credits" not in text
    assert len(text.encode("utf-16-le")) // 2 < 4096


@pytest.mark.anyio
async def test_cotas_reports_kiro_limits_as_unavailable_without_running_it(
    tmp_path, monkeypatch
):
    cfg, transport, _, entries, _ = _quota_config(tmp_path, monkeypatch, ("kiro",))
    await _send(cfg, "/cotas")
    text = transport.send_calls[-1]["message"].text
    assert "Kiro — SEM DADOS" in text
    assert "não informa janelas de 5h/semanal" in text
    assert not any(entry.runner.calls for entry in entries)


@pytest.mark.anyio
async def test_cotas_names_unconfigured_engines_and_skips_their_refresh(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(quota_command, "latest_codex_quota", lambda: calls.append(1))
    cfg, transport, _, _, _ = _quota_config(
        tmp_path, monkeypatch, ("claude", "codex"), missing=("codex",)
    )
    await _send(cfg, "/cotas")
    text = transport.send_calls[-1]["message"].text
    assert "Claude — SEM DADOS" in text
    assert "Codex — CLI ausente" in text and "Agy — não configurado" in text
    assert calls == []


@pytest.mark.anyio
async def test_cotas_without_store_replies_instead_of_failing(tmp_path, monkeypatch):
    cfg, transport, _, _, _ = _quota_config(tmp_path, monkeypatch, ("codex",))
    cfg = replace(cfg, exec_cfg=replace(cfg.exec_cfg, quota_store=None))
    await _send(cfg, "/cotas")
    assert "não está disponível" in transport.send_calls[-1]["message"].text


def _replies(transport) -> list[str]:
    return [
        call["message"].text
        for call in transport.send_calls
        if call["options"] and call["options"].reply_to
    ]


@pytest.mark.anyio
async def test_cotas_uses_the_snapshot_when_the_codex_read_fails(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(quota_command, "latest_codex_quota", broken)
    cfg, transport, store, _, _ = _quota_config(tmp_path, monkeypatch, ("codex",))
    store.observe("codex", Telemetry(five_hour=Quota(10, time.time() + HOUR)), None)
    await _send(cfg, "/cotas")
    (text,) = _replies(transport)
    assert "Codex — DISPONÍVEL" in text and "5h: 10% usado" in text


@pytest.mark.anyio
async def test_slow_codex_read_times_out_and_is_not_started_twice(
    tmp_path, monkeypatch
):
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        release.wait(5)

    monkeypatch.setattr(quota_command, "latest_codex_quota", slow)
    monkeypatch.setattr(quota_command, "_CODEX_TIMEOUT_S", 0.05)
    cfg, transport, store, _, _ = _quota_config(tmp_path, monkeypatch, ("codex",))
    store.observe("codex", Telemetry(five_hour=Quota(10, time.time() + HOUR)), None)
    try:
        with anyio.fail_after(3):
            await _send(cfg, "/cotas")  # Gives up on the read and answers.
            await _send(cfg, "/cotas")  # That read is still running.
    finally:
        release.set()
    assert calls == [1]
    texts = _replies(transport)
    assert len(texts) == 2 and all("5h: 10% usado" in text for text in texts)
    with anyio.fail_after(2):
        while quota_command._CODEX_READING.locked():
            await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_cotas_failure_is_reported_without_stopping_the_loop(
    tmp_path, monkeypatch
):
    def broken(*_args):
        raise ValueError("render")

    monkeypatch.setattr(quota_command, "render_quota_report", broken)
    cfg, transport, _, _, _ = _quota_config(tmp_path, monkeypatch, ("claude",))
    await _send(cfg, "/cotas")
    (text,) = _replies(transport)
    assert "Não foi possível mostrar as cotas agora." in text


def test_menu_lists_cotas_ahead_of_project_aliases(tmp_path, monkeypatch):
    cfg, _, _, _, _ = _quota_config(tmp_path, monkeypatch, ("codex",))
    commands = [entry["command"] for entry in build_bot_commands(cfg.runtime)]
    assert "cotas" in commands and "quota" not in commands
    assert commands.index("cotas") == commands.index("codex") + 1


@pytest.mark.parametrize(
    ("quota", "expected"),
    [
        (None, "5h: indisponível"),
        (Quota(100, 999), "5h: aguardando atualização"),
        (Quota(0), "5h: 0% usado"),
        (Quota(42.4, 1000 + 1), "5h: 42% usado · renova em 1min"),
        (Quota(42, 1000 + 2 * HOUR + 600), "5h: 42% usado · renova em 2h 10min"),
        (Quota(61, 1000 + 3 * DAY + 4 * HOUR), "5h: 61% usado · renova em 3d 4h"),
    ],
)
def test_quota_line_keeps_the_footer_wording(quota, expected):
    assert quota_line("5h", quota, 1000) == expected
