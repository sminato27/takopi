"""Kiro 3.0.0 protocol replay; no model or Telegram calls."""

import json
from pathlib import Path
import sys
import textwrap

import anyio
import pytest

from takopi.config import ConfigError
from takopi.engines import get_backend, list_backend_ids
from takopi.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from takopi.runners.kiro import KiroRunner, build_runner
from takopi.runners.run_options import EngineRunOptions, apply_run_options
from takopi.schemas.kiro import decode_event
from takopi.telemetry import telemetry_lines
from takopi.telegram.engine_overrides import allowed_reasoning_levels

FIXTURE = Path(__file__).parent / "fixtures/kiro/headless.jsonl"
TOKEN = ResumeToken("kiro", "sess_test")


def update(kind, *, sid="sess_test", **fields):
    return {
        "type": "sessionUpdate",
        "data": {
            "sessionId": sid,
            "update": {"sessionUpdate": kind, **fields},
        },
    }


def finish(text="done", *, status="success", sid="sess_test", **fields):
    return {
        "type": "runFinished",
        "data": {
            "sessionId": sid,
            "status": status,
            "stopReason": "end_turn",
            "finalText": text,
            **fields,
        },
    }


def fake_cli(tmp_path, records, *, rc=0, wait=False):
    (tmp_path / "stream").write_text("\n".join(json.dumps(r) for r in records))
    script = tmp_path / "kiro-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(f"""
        import json, pathlib, signal, sys, time
        root = pathlib.Path({str(tmp_path)!r})
        (root / 'input').write_text(sys.stdin.read())
        (root / 'args').write_text(json.dumps(sys.argv[1:]))
        def stop(*args):
            (root / 'terminated').touch()
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        for line in (root / 'stream').read_text().splitlines():
            print(line, flush=True)
        (root / 'ready').touch()
        if {wait!r}:
            while True: time.sleep(0.01)
        sys.exit({rc})
    """)
    )
    script.chmod(0o700)
    return str(script)


@pytest.mark.anyio
@pytest.mark.parametrize("resume", [None, TOKEN])
async def test_recorded_stream_delivers_answer_progress_and_context(tmp_path, resume):
    records = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    runner = KiroRunner(kiro_cmd=fake_cli(tmp_path, records))
    prompt = "--resume-id evil; $(touch NEVER)\nLeia o anexo incoming/file.txt"
    events = [event async for event in runner.run(prompt, resume)]
    assert sum(isinstance(e, StartedEvent) for e in events) == 1
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    completed = events[-1]
    assert isinstance(completed, CompletedEvent) and completed.ok
    assert completed.answer == "KIRO_FILE_7c3e" and completed.resume == TOKEN
    actions = [
        e for e in events if isinstance(e, ActionEvent) and e.action.kind != "telemetry"
    ]
    assert [e.phase for e in actions] == ["started", "updated", "completed"]
    assert actions[-1].ok
    assert completed.usage is not None
    status = completed.usage["telemetry"]
    assert status.model == "auto" and status.context_percent == pytest.approx(2.8669)
    assert "Contexto: 3% usado" in telemetry_lines(status)
    assert status.five_hour is None and status.weekly is None
    assert (tmp_path / "input").read_text() == prompt
    args = json.loads((tmp_path / "args").read_text())
    assert prompt not in args and "--v3" in args
    assert "--trust-all-tools" not in args
    assert ("--resume-id" in args) == bool(resume)


def test_registration_resume_and_overrides():
    assert "kiro" in list_backend_ids()
    assert get_backend("kiro").cli_cmd == "kiro-cli"
    runner = KiroRunner(model="default", effort="high", agent="custom")
    with apply_run_options(EngineRunOptions(model="selected", reasoning="low")):
        args = runner.build_args("text", TOKEN, state=None)
        state = runner.new_state("text", TOKEN)
    assert args[args.index("--model") + 1] == "selected"
    assert args[args.index("--effort") + 1] == "low"
    assert args[args.index("--agent") + 1] == "custom"
    assert args[args.index("--resume-id") + 1] == TOKEN.value
    assert state.telemetry.model == "selected" and state.telemetry.effort == "low"
    assert runner.extract_resume(runner.format_resume(TOKEN)) == TOKEN
    assert runner.extract_resume("`kiro-cli chat --resume-id invalid/path`") is None
    for token in (ResumeToken("codex", "sess_test"), ResumeToken("kiro", "$(bad)")):
        with pytest.raises(RuntimeError):
            runner.format_resume(token)
    assert "none" in allowed_reasoning_levels("kiro")
    assert "--trust-all-tools" in KiroRunner(trust_all_tools=True).build_args(
        "", None, state=None
    )


def test_claude_pipeline_agent_auto_selected_for_anthropic_models():
    # No agent configured: an Anthropic model gets the ported subagent pipeline.
    args = KiroRunner(model="claude-opus-5.5").build_args("text", None, state=None)
    assert args[args.index("--agent") + 1] == "claude-pipeline"

    # A non-Claude model keeps Kiro's own default agent (no --agent injected).
    args = KiroRunner(model="gpt-5.6-sol").build_args("text", None, state=None)
    assert "--agent" not in args

    # An explicitly configured agent always wins, regardless of model.
    runner = KiroRunner(model="claude-opus-5.5", agent="custom")
    args = runner.build_args("text", None, state=None)
    assert args[args.index("--agent") + 1] == "custom"

    # The /model override from Telegram is also checked, not just the static config.
    with apply_run_options(EngineRunOptions(model="claude-sonnet-5.5")):
        args = KiroRunner().build_args("text", None, state=None)
    assert args[args.index("--agent") + 1] == "claude-pipeline"


@pytest.mark.parametrize(
    "config",
    [
        {"ai_memory": True},
        {"model": 1},
        {"agent": ""},
        {"effort": "invalid"},
        {"trust_all_tools": "true"},
    ],
)
def test_invalid_configuration_is_rejected(config, tmp_path):
    with pytest.raises(ConfigError):
        build_runner(config, tmp_path / "takopi.toml")


def test_configuration_preserves_cli_defaults(tmp_path):
    runner = build_runner({}, tmp_path / "takopi.toml")
    assert isinstance(runner, KiroRunner)
    assert runner.model is None and runner.effort is None and not runner.trust_all_tools


@pytest.mark.anyio
@pytest.mark.parametrize(
    "end,rc,expected",
    [
        (finish("", status="success"), 0, "sem uma resposta"),
        (
            {
                "type": "runError",
                "data": {"stage": "interrupted", "message": "Cancelled"},
            },
            1,
            "Cancelled",
        ),
        (
            finish(status="failed", message="Workflow incomplete"),
            1,
            "Workflow incomplete",
        ),
        (finish(), 1, "rc=1"),
        (None, 0, "without a result"),
    ],
)
async def test_failure_paths_never_report_success(tmp_path, end, rc, expected):
    records = [update("config_option_update", configOptions=[])]
    if end is not None:
        records.append(end)
    runner = KiroRunner(kiro_cmd=fake_cli(tmp_path, records, rc=rc))
    events = [event async for event in runner.run("go", TOKEN)]
    completed = events[-1]
    assert isinstance(completed, CompletedEvent) and not completed.ok
    assert completed.error is not None and expected in completed.error
    assert completed.resume == TOKEN


@pytest.mark.anyio
async def test_init_error_surfaces_without_session(tmp_path):
    runner = KiroRunner(
        kiro_cmd=fake_cli(
            tmp_path,
            [
                {
                    "type": "runError",
                    "data": {
                        "sessionId": None,
                        "stage": "init",
                        "message": "Login required",
                    },
                }
            ],
            rc=1,
        )
    )
    events = [event async for event in runner.run("go", None)]
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.error == "Login required"
    assert completed.resume is None


@pytest.mark.anyio
async def test_mismatched_session_stops_instead_of_mixing_answers(tmp_path):
    runner = KiroRunner(
        kiro_cmd=fake_cli(tmp_path, [update("future"), finish("foreign", sid="other")])
    )
    with pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="another session")):
        _ = [event async for event in runner.run("go", TOKEN)]


def test_replay_thoughts_intermediate_boundaries_and_unknown_updates():
    runner = KiroRunner()
    state = runner.new_state("", None)

    def translate(record):
        return runner.translate(
            decode_event(json.dumps(record).encode()),
            state=state,
            resume=None,
            found_session=state.factory.resume,
        )

    translate(
        update(
            "agent_message_chunk",
            content={"type": "text", "text": "old"},
            _meta={"kiro": {"replay": True}},
        )
    )
    translate(
        update("agent_thought_chunk", content={"type": "text", "text": "private"})
    )
    translate(
        update(
            "config_option_update",
            configOptions=[
                {"id": "model", "currentValue": "model"},
                {"id": "effortLevel", "currentValue": "low"},
            ],
        )
    )
    assert state.telemetry.effort == "low"
    assert (
        translate(update("session_info_update", _meta={"kiro": {"kind": "turn_end"}}))
        == []
    )
    assert translate(update("future")) == []
    translate(
        update(
            "agent_message_chunk", content={"type": "text", "text": "complete answer"}
        )
    )
    completed = translate(finish("complete", finalTextTruncated=True))[-1]
    assert completed.answer == "complete answer"
    assert "old" not in completed.answer and "private" not in completed.answer


@pytest.mark.anyio
async def test_cancel_terminates_the_cli(tmp_path):
    runner = KiroRunner(kiro_cmd=fake_cli(tmp_path, [update("future")], wait=True))
    events = []

    async def consume():
        async for event in runner.run("go", None):
            events.append(event)  # noqa: PERF401 - inspect events before cancellation

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            while not events or not (tmp_path / "ready").exists():
                await anyio.sleep(0.01)
            group.cancel_scope.cancel()
    assert (tmp_path / "terminated").exists()
    assert not any(isinstance(e, CompletedEvent) for e in events)
