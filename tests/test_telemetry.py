import json
from contextlib import closing
import sqlite3
import time

import pytest

from takopi.markdown import render_event_cli
from takopi.model import Action, ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from takopi.progress import ProgressTracker
from takopi.runners.agy import AgyRunner
from takopi.runners.agy_telemetry import apply_reports, refresh_agy
from takopi.runners.claude import (
    ClaudeRunner,
    ClaudeStreamState,
    translate_claude_event,
)
from takopi.runners.codex_telemetry import CodexTelemetryReader
from takopi.runners.run_options import EngineRunOptions, apply_run_options
from takopi.schemas.claude import decode_stream_json_line
from takopi.telemetry import (
    Quota,
    Telemetry,
    number,
    telemetry_event,
    telemetry_lines,
    token_count,
)
from takopi.telegram.bridge import TelegramPresenter


def _record(kind, payload, date="2026-10-29T12:00:00Z"):
    return json.dumps({"timestamp": date, "type": kind, "payload": payload}) + "\n"


def _rollout(tmp_path, session="sid"):
    path = tmp_path / "sessions" / "2026" / f"rollout-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_record("session_meta", {"id": session}))
    return path


def _codex_usage(tokens=12345, cumulative=999999):
    return _record(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": tokens},
                "total_token_usage": {"total_tokens": cumulative},
                "model_context_window": 258400,
            },
            "rate_limits": {
                "primary": {
                    "used_percent": 0,
                    "window_minutes": 300,
                    "resets_at": 9999999999,
                },
                "secondary": {
                    "used_percent": 33,
                    "window_minutes": 10080,
                    "resets_at": 9999999999,
                },
            },
        },
    )


def test_codex_current_turn_exact_session_incremental_and_incomplete_records(tmp_path):
    path = _rollout(tmp_path)
    with path.open("a") as stream:
        stream.write(
            _record(
                "turn_context",
                {"model": "old", "effort": "low"},
                "2026-10-28T12:00:00Z",
            )
        )
        stream.write(
            _record("turn_context", {"model": "gpt-6-astra", "effort": "xhigh"})
        )
        stream.write(_codex_usage())
        stream.write('{"type":')
    reader = CodexTelemetryReader(home=tmp_path, since=1793232000)
    result = reader.read("sid")
    assert result.model == "gpt-6-astra" and result.effort == "xhigh"
    assert result.context_tokens == 12345 and result.context_window == 258400
    assert result.five_hour is not None and result.weekly is not None
    assert result.five_hour.used_percent == 0 and result.weekly.used_percent == 33
    assert reader.read("sid") == result
    with path.open("a") as stream:
        stream.write('"ignored"}\n')
        stream.write(_codex_usage(500))
    assert reader.read("sid").context_tokens == 500  # compaction can reduce usage
    fresh = CodexTelemetryReader(home=tmp_path, since=9999999999)
    assert fresh.read("sid") == Telemetry()  # no previous-turn figures
    assert CodexTelemetryReader(home=tmp_path, since=0).read("different") == Telemetry()
    assert CodexTelemetryReader(home=tmp_path, since=0).read("../*") == Telemetry()


def test_codex_readonly_index_and_misassociated_rollout(tmp_path):
    path = _rollout(tmp_path)
    path.write_text(path.read_text() + _codex_usage())
    dbpath = tmp_path / "state_5.sqlite"
    with closing(sqlite3.connect(dbpath)) as db:
        db.execute("CREATE TABLE threads(id TEXT, rollout_path TEXT)")
        db.executemany(
            "INSERT INTO threads VALUES(?,?)",
            [("sid", str(path)), ("wrong", str(path))],
        )
        db.commit()
    before = dbpath.read_bytes()
    assert (
        CodexTelemetryReader(home=tmp_path, since=0).read("sid").context_tokens == 12345
    )
    assert CodexTelemetryReader(home=tmp_path, since=0).read("wrong") == Telemetry()
    assert dbpath.read_bytes() == before


def test_codex_malformed_and_large_content_does_not_hide_status(tmp_path):
    path = _rollout(tmp_path)
    path.write_text(path.read_text() + _codex_usage())
    reader = CodexTelemetryReader(home=tmp_path, since=0)
    reader.read("sid")
    with path.open("a") as stream:
        stream.write(json.dumps({"text": "x" * 2_000_000}) + "\n")
        stream.write("not-json\n[]\n")
        stream.write(_codex_usage(900))
    assert reader.read("sid").context_tokens == 900
    path.unlink()
    assert reader.read("sid").context_tokens == 900


def _claude(state, data):
    return translate_claude_event(
        decode_stream_json_line(json.dumps(data)),
        title="Claude",
        state=state,
        factory=state.factory,
    )


def _claude_init(state):
    return _claude(
        state,
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sid",
            "model": "sonnet",
            "effort": {"level": "high"},
        },
    )


def _assistant(model="sonnet", parent=None, usage=None):
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [],
            "usage": usage
            or {
                "input_tokens": 10,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 30,
                "output_tokens": 999,
            },
        },
    }


def _claude_result():
    return {
        "type": "result",
        "subtype": "success",
        "session_id": "sid",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "result": "OK",
        "usage": {"input_tokens": 999999},
        "modelUsage": {
            "sonnet": {"contextWindow": 1000000},
            "haiku": {"contextWindow": 200000},
        },
    }


def _limits(five=0.25, weekly=0.5, session="sid"):
    return {
        "type": "rate_limit_event",
        "session_id": session,
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {"utilization": five, "resetsAt": 9999999999},
                "seven_day": {"utilization": weekly, "resetsAt": 9999999999},
            }
        },
    }


def test_claude_main_context_cache_subagents_model_switch_and_compaction():
    state = ClaudeStreamState()
    tracker = ProgressTracker(engine="claude")
    for event in (
        _claude_init(state)
        + _claude(state, _assistant())
        + _claude(state, _assistant("haiku", "tool-subagent"))
        + _claude(state, _limits())
        + _claude(state, _claude_result())
    ):
        tracker.note_event(event)
    status = tracker.snapshot().telemetry
    assert (
        status is not None
        and status.five_hour is not None
        and status.weekly is not None
    )
    assert status.model == "sonnet" and status.effort == "high"
    assert status.context_tokens == 240 and status.context_window == 1000000
    assert status.five_hour.used_percent == 25 and status.weekly.used_percent == 50
    assert tracker.action_count == 0
    assert _claude(state, _limits(session="other")) == []
    _claude(state, {"type": "system", "subtype": "compact_boundary"})
    assert state.telemetry.context_tokens is None
    _claude(state, _assistant("other-model"))
    assert (
        state.telemetry.model == "other-model"
        and state.telemetry.context_window is None
    )


@pytest.mark.anyio
async def test_claude_late_quota_is_delivered_before_single_final(tmp_path):
    executable = tmp_path / "claude-fake"
    records = [
        {"type": "system", "subtype": "init", "session_id": "sid", "model": "sonnet"},
        _assistant(),
        _claude_result(),
        _limits(five=0.75),
        _claude_result(),
    ]
    executable.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stdout.write("
        + repr("".join(json.dumps(r) + "\n" for r in records))
        + ")\n"
    )
    executable.chmod(0o700)
    runner = ClaudeRunner(claude_cmd=str(executable))
    with apply_run_options(EngineRunOptions(reasoning="max")):
        events = [event async for event in runner.run("test", None)]
    assert sum(isinstance(e, StartedEvent) for e in events) == 1
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    final = events[-1]
    assert isinstance(final, CompletedEvent)
    assert final.answer == "OK"
    tracker = ProgressTracker(engine="claude")
    for event in events:
        tracker.note_event(event)
    assert tracker.telemetry is not None and tracker.telemetry.five_hour is not None
    assert tracker.telemetry.five_hour.used_percent == 75
    assert tracker.telemetry.effort == "max"
    assert tracker.telemetry.context_tokens == 240


def _agy_reports():
    return {
        "groups": [
            {
                "buckets": [
                    {
                        "id": "gemini-5h",
                        "remaining_fraction": 0.8,
                        "reset_time": "2099-01-01T00:00:00Z",
                    },
                    {"id": "gemini-weekly", "remaining_fraction": 0.6},
                ]
            },
            {
                "buckets": [
                    {"id": "3p-5h", "remaining_fraction": 0.1},
                    {"id": "3p-weekly", "remaining_fraction": 0.2},
                ]
            },
        ]
    }


@pytest.mark.parametrize(
    "family,expected,group",
    [("gemini-flash", 20, "Gemini"), ("claude-sonnet", 90, "Claude/GPT")],
)
def test_agy_selects_current_model_quota_group(family, expected, group):
    result = apply_reports(
        Telemetry(last_call_tokens=800),
        {"id": family, "label": "actual", "effort": "high"},
        _agy_reports(),
    )
    assert result.five_hour is not None
    assert result.five_hour.used_percent == pytest.approx(expected)
    assert (
        result.quota_group == group
        and result.model == "actual"
        and result.effort == "high"
    )
    assert (
        result.context_tokens is None
        and result.context_window is None
        and result.last_call_tokens == 800
    )


def test_agy_missing_unknown_and_malformed_quota_never_become_zero():
    assert apply_reports(Telemetry(), {}, _agy_reports()).five_hour is None
    assert (
        apply_reports(Telemetry(), {"id": "unknown"}, _agy_reports()).five_hour is None
    )
    assert (
        apply_reports(
            Telemetry(),
            {"id": "gemini-flash"},
            {
                "groups": [
                    None,
                    {"buckets": [None, {"id": [], "remaining_fraction": 1}]},
                ]
            },
        ).five_hour
        is None
    )
    assert apply_reports(Telemetry(), {"id": "gemini-flash"}, {}).weekly is None


@pytest.mark.anyio
async def test_agy_refresh_passes_exact_session_and_overrides_without_shell(
    monkeypatch,
):
    import takopi.runners.agy_telemetry as module
    from subprocess import CompletedProcess

    calls = []

    async def run(args, **kwargs):
        calls.append(args)
        command = args[-1][1:]
        data = (
            {"id": "gemini-flash", "label": "Gemini Flash", "effort": "high"}
            if command == "model"
            else _agy_reports()
        )
        return CompletedProcess(
            args,
            0,
            json.dumps({"command": {"name": command, "data": data}}).encode(),
            b"",
        )

    monkeypatch.setattr(module.anyio, "run_process", run)
    result = await refresh_agy(
        Telemetry(),
        executable="agy",
        session="sid",
        model="model; literal",
        effort="high",
    )
    assert result.five_hour is not None
    assert len(calls) == 2 and result.five_hour.used_percent == pytest.approx(20)
    assert all(call[call.index("--conversation") + 1] == "sid" for call in calls)
    assert all(call[call.index("--model") + 1] == "model; literal" for call in calls)

    async def unavailable(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(module.anyio, "run_process", unavailable)
    assert (
        await refresh_agy(Telemetry(), executable="agy", session="sid") == Telemetry()
    )


def test_agy_step_counts_are_not_cumulative_or_checkpoint_usage():
    runner = AgyRunner()
    state = runner.new_state("", None)
    for kind, usage in [
        (
            "agent_response",
            {"input_tokens": 10, "cache_read_tokens": 100, "output_tokens": 20},
        ),
        ("checkpoint", {"input_tokens": 999}),
    ]:
        runner.translate(
            {
                "event": "step_update",
                "step_update": {"step_type": kind, "step_index": 1, "usage": usage},
            },
            state=state,
            resume=None,
            found_session=None,
        )
    assert state.telemetry.last_call_tokens == 130
    assert state.telemetry.context_window is None


@pytest.mark.parametrize(
    "value", [True, False, -1, "0", float("inf"), float("nan"), None]
)
def test_invalid_metrics_are_unavailable(value):
    assert number(value) is None
    assert Quota.from_values(value, None) is None
    assert token_count({"input_tokens": value}, ("input_tokens",)) is None


def test_footer_distinguishes_missing_zero_and_expired_and_does_not_count_actions():
    now = 1000
    status = Telemetry(
        model="gpt`\ninjected",
        effort="high",
        context_tokens=100,
        context_window=1000,
        five_hour=Quota(0, now + 60),
        weekly=Quota(100, now - 1),
    )
    lines = "\n".join(telemetry_lines(status, now=now))
    assert (
        "10% usado" in lines and "5h: 0% usado" in lines and "renova em 1min" in lines
    )
    assert "Semanal: aguardando atualização" in lines and "100% usado" not in lines
    assert "gptinjected" not in lines and "gpt injected" in lines
    blank = "\n".join(telemetry_lines(Telemetry(), now=now))
    assert "indisponível" in blank and "0%" not in blank
    tracker = ProgressTracker(engine="codex")
    event = telemetry_event("codex", status)
    tracker.note_event(event)
    assert tracker.action_count == 0 and tracker.snapshot().actions == ()
    assert render_event_cli(event) == []
    assert ProgressTracker(engine="codex").telemetry == Telemetry()
    assert ProgressTracker(engine="pi").telemetry is None
    assert (
        tracker.note_event(
            ActionEvent(
                engine="codex",
                action=Action("t", "telemetry", "unknown"),
                phase="updated",
            )
        )
        is False
    )


@pytest.mark.parametrize("overflow", ["trim", "split"])
def test_telegram_status_keeps_resume_and_utf16_limit_in_long_answers(overflow):
    tracker = ProgressTracker(engine="codex")
    tracker.set_resume(ResumeToken("codex", "sid"))
    tracker.note_event(
        telemetry_event(
            "codex",
            Telemetry(
                model="m" * 300, effort="high", five_hour=Quota(30, time.time() + 500)
            ),
        )
    )
    state = tracker.snapshot(
        resume_formatter=lambda r: f"`codex resume {r.value}`",
        context_line="project: " + "p" * 150,
    )
    rendered = TelegramPresenter(message_overflow=overflow).render_final(
        state, elapsed_s=1, status="done", answer="😀" * 5000
    )
    messages = [rendered, *rendered.extra.get("followups", [])]
    assert len(messages) > 1 if overflow == "split" else len(messages) == 1
    for message in messages:
        assert len(message.text.encode("utf-16-le")) // 2 <= 4096
        assert "codex resume sid" in message.text and "Modelo:" in message.text
        assert (
            "5h: 30% usado" in message.text and "Semanal: indisponível" in message.text
        )
