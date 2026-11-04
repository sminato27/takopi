"""Replay headless background turns without spending a model call.

The empty zero-turn result on resume is documented in anthropics/
claude-agent-sdk-python#1463. Background work can produce further turns
before the print-mode process exits.
"""

import json
from pathlib import Path
import textwrap

import anyio
import pytest

from takopi.managed import AiMemoryConfig
from takopi.markdown import MarkdownPresenter
from takopi.model import ActionEvent, CompletedEvent, ResumeToken, StartedEvent
from takopi.progress import ProgressTracker
from takopi.runner_bridge import ExecBridgeConfig, IncomingMessage, handle_message
from takopi.runners.claude import ClaudeRunner
from tests.telegram_fakes import FakeTransport

WAITING = "Resposta recebida; aguardando tarefas em segundo plano: "


def result(text="", *, turns=1, error=False):
    return {
        "type": "result",
        "subtype": "error_during_execution" if error else "success",
        "session_id": "session",
        "result": text,
        "is_error": error,
        "num_turns": turns,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "modelUsage": {"claude-test": {"contextWindow": 1000000}},
    }


def assistant(content, *, parent=None):
    return {
        "type": "assistant",
        "session_id": "session",
        "parent_tool_use_id": parent,
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": content,
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 295273,
                "cache_creation_input_tokens": 516,
            },
        },
    }


INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "session",
    "model": "claude-test",
    "permissionMode": "auto",
}


def replay_cli(tmp_path: Path, records, *, exit_code=0, wait=False):
    data = tmp_path / "stream.jsonl"
    data.write_text("\n".join(json.dumps(row) for row in records))
    executable = tmp_path / "claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(f"""
            import os, pathlib, signal, sys, time
            root = pathlib.Path({str(tmp_path)!r})
            (root / 'ceiling').write_text(os.environ.get('CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS', 'unset'))
            for line in (root / 'stream.jsonl').read_text().splitlines():
                print(line, flush=True)
            def stop(*_):
                (root / 'terminated').touch()
                sys.exit(0)
            signal.signal(signal.SIGTERM, stop)
            (root / 'ready').touch()
            if {wait!r}:
                while True:
                    time.sleep(0.01)
            (root / 'finalized').touch()
            sys.exit({exit_code})
        """)
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.anyio
@pytest.mark.parametrize("managed", [False, True])
async def test_resume_and_background_turns_deliver_latest_result_and_progress(
    tmp_path, monkeypatch, managed
):
    # A Claude session launched by Takopi passes its own ceiling to child shells.
    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    records = [
        INIT,
        result(turns=0),
        INIT,
        assistant([{"type": "text", "text": "Retomei o agente auxiliar."}]),
        result("Retomei o agente auxiliar."),
        assistant(
            [
                {
                    "type": "tool_use",
                    "id": "edit",
                    "name": "Edit",
                    "input": {"file_path": "example.py"},
                }
            ]
        ),
        {
            "type": "user",
            "session_id": "session",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "edit", "content": "saved"}
                ],
            },
        },
        assistant([{"type": "text", "text": "Implementação e testes concluídos."}]),
        result("Implementação e testes concluídos."),
        result(turns=0),  # A later housekeeping cycle must not erase the answer.
        {
            "type": "rate_limit_event",
            "session_id": "session",
            "rate_limit_info": {
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.53, "resetsAt": 2000000000}
                }
            },
        },
    ]
    executable = replay_cli(tmp_path, records)
    runner = ClaudeRunner(claude_cmd=str(executable))
    if managed:
        runner.ai_memory = AiMemoryConfig(
            str(executable), tmp_path / "sessions.sqlite3"
        )
    tracker = ProgressTracker(engine="claude")
    events = []
    async for event in runner.run("continue", None):
        if isinstance(event, CompletedEvent):
            assert (tmp_path / "finalized").exists()
        tracker.note_event(event)
        events.append(event)
    assert sum(isinstance(e, StartedEvent) for e in events) == 1
    assert sum(isinstance(e, CompletedEvent) for e in events) == 1
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok and completed.answer == "Implementação e testes concluídos."
    assert completed.resume == ResumeToken("claude", "session")
    assert any(
        isinstance(e, ActionEvent) and e.action.kind == "file_change" for e in events
    )
    assert tracker.telemetry is not None and tracker.telemetry.five_hour is not None
    assert tracker.telemetry.context_tokens == 295791
    assert tracker.telemetry.context_window == 1000000
    assert tracker.telemetry.five_hour.used_percent == pytest.approx(53)
    assert (tmp_path / "ceiling").read_text() == "3600000"
    # One note per answered turn, kept as a single progress line.
    waits = [
        e.action.title
        for e in events
        if isinstance(e, ActionEvent) and e.action.id == "claude.background_wait"
    ]
    assert waits == [
        f"{WAITING}Retomei o agente auxiliar.",
        f"{WAITING}Implementação e testes concluídos.",
    ]
    assert [
        a.action.title
        for a in tracker.snapshot().actions
        if a.action.id == "claude.background_wait"
    ] == waits[-1:]


@pytest.mark.anyio
async def test_late_error_is_not_hidden_by_earlier_success(tmp_path):
    executable = replay_cli(
        tmp_path,
        [INIT, result("Aguardando agente."), result("Usage limit reached", error=True)],
        exit_code=1,
    )
    events = [e async for e in ClaudeRunner(claude_cmd=str(executable)).run("go", None)]
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert not completed.ok
    assert completed.error == "Usage limit reached"
    assert completed.answer == "Aguardando agente."


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("records", "body"),
    [
        ([INIT, result("Usage limit reached", error=True)], "Usage limit reached"),
        (
            [
                INIT,
                result("Aguardando agente."),
                result("Usage limit reached", error=True),
            ],
            "Aguardando agente.\n\nUsage limit reached",
        ),
    ],
    ids=["no-answer", "after-answer"],
)
async def test_final_message_shows_error_once(tmp_path, records, body):
    executable = replay_cli(tmp_path, records, exit_code=1)
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    await handle_message(
        cfg,
        runner=ClaudeRunner(claude_cmd=str(executable)),
        incoming=IncomingMessage(channel_id=1, message_id=1, text="go"),
        resume_token=None,
    )
    final = transport.send_calls[-1]["message"].text
    assert final.count("Usage limit reached") == 1
    assert f"\n\n{body}\n\n" in final


@pytest.mark.anyio
@pytest.mark.parametrize("turns", [0, 1])
async def test_empty_result_reports_missing_answer(tmp_path, turns):
    executable = replay_cli(tmp_path, [INIT, result(turns=turns)])
    events = [e async for e in ClaudeRunner(claude_cmd=str(executable)).run("go", None)]
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert not completed.ok and completed.error


@pytest.mark.anyio
async def test_empty_result_after_answer_keeps_that_answer(tmp_path):
    executable = replay_cli(tmp_path, [INIT, result("Aguardando agente."), result()])
    events = [e async for e in ClaudeRunner(claude_cmd=str(executable)).run("go", None)]
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.ok and completed.answer == "Aguardando agente."


@pytest.mark.anyio
async def test_fallback_uses_main_assistant_not_subagent_text(tmp_path):
    executable = replay_cli(
        tmp_path,
        [
            INIT,
            result("Aguardando agente."),
            assistant([{"type": "text", "text": "Resposta principal."}]),
            assistant(
                [{"type": "text", "text": "Rascunho do auxiliar."}], parent="agent-tool"
            ),
            result(),
        ],
    )
    events = [e async for e in ClaudeRunner(claude_cmd=str(executable)).run("go", None)]
    completed = events[-1]
    assert isinstance(completed, CompletedEvent)
    assert completed.answer == "Resposta principal."


@pytest.mark.anyio
async def test_background_wait_remains_cancellable(tmp_path):
    records = [
        INIT,
        result("Aguardando agente."),
        # More background actions than the five-line progress window holds.
        *(
            assistant(
                [
                    {
                        "type": "tool_use",
                        "id": f"t{i}",
                        "name": "Bash",
                        "input": {"command": "x"},
                    }
                ]
            )
            for i in range(6)
        ),
    ]
    executable = replay_cli(tmp_path, records, wait=True)
    transport = FakeTransport()
    cfg = ExecBridgeConfig(
        transport=transport, presenter=MarkdownPresenter(), final_notify=True
    )
    running_tasks = {}
    note = f"{WAITING}Aguardando agente."

    def shown():
        return [call["message"].text for call in transport.edit_calls]

    async def run_handle_message():
        await handle_message(
            cfg,
            runner=ClaudeRunner(claude_cmd=str(executable)),
            incoming=IncomingMessage(channel_id=1, message_id=1, text="go"),
            resume_token=None,
            running_tasks=running_tasks,
        )

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(run_handle_message)
            # Claude is idle and the bridge has shown the note and all six tools.
            while not (
                (tmp_path / "ready").exists()
                and any("step 7" in text for text in shown())
            ):
                await anyio.sleep(0.01)
            assert note in shown()[-1]  # The answer is visible while Claude waits.
            (task,) = running_tasks.values()
            task.cancel_requested.set()  # What /stop does for a running task.
    assert len(transport.send_calls) == 1  # Progress only: nothing completed.
    assert "cancelled" in shown()[-1] and note in shown()[-1]
    assert (tmp_path / "terminated").exists()


def test_background_wait_note_truncates_long_answers():
    note = ClaudeRunner().background_wait_event("x" * 250)
    assert note.action.title == f"{WAITING}{'x' * 200}…"


@pytest.mark.parametrize("billing", [False, True])
def test_background_wait_respects_explicit_environment_and_billing(
    monkeypatch, billing
):
    monkeypatch.setenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "1200000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    env = ClaudeRunner(use_api_billing=billing).env(state=None)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "1200000"
    assert ("ANTHROPIC_API_KEY" in env) == billing
