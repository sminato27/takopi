from pathlib import Path

import pytest

from takopi.config import ConfigError
from takopi.managed import AiMemoryConfig
from takopi.model import CompletedEvent, ResumeToken, StartedEvent
from takopi.runners.agy import AgyRunner, build_runner
from takopi.runners.run_options import EngineRunOptions, apply_run_options


def test_agy_model_effort_prompt_and_exact_resume_are_separate_arguments():
    runner = AgyRunner()
    token = ResumeToken("agy", "sid")
    prompt = "--conversation evil; $(touch NEVER)"
    with apply_run_options(
        EngineRunOptions(model="Gemini 3.8 Flash (High)", reasoning="max")
    ):
        args = runner.build_args(prompt, token, state=None)
    assert args[args.index("--conversation") + 1] == "sid"
    assert args[args.index("--model") + 1] == "Gemini 3.8 Flash (High)"
    assert args[args.index("--effort") + 1] == "max"
    assert args[-2:] == ["-p", prompt]
    assert runner.stdin_payload(prompt, token, state=None) is None
    assert runner.extract_resume(runner.format_resume(token)) == token
    with pytest.raises(RuntimeError):
        runner.format_resume(ResumeToken("claude", "sid"))
    with pytest.raises(ConfigError):
        build_runner({}, Path("takopi.toml"))


@pytest.mark.parametrize(
    "status,sid,ok",
    [
        ("SUCCESS", "sid", True),
        ("ERROR", "sid", False),
        ("SUCCESS", "wrong", False),
        (None, "sid", False),
    ],
)
def test_agy_result_requires_success_and_the_started_session(status, sid, ok):
    runner = AgyRunner()
    state = runner.new_state("", None)

    def event(data):
        return runner.translate(
            data, state=state, resume=None, found_session=state.factory.resume
        )

    started = event(
        {
            "event": "init",
            "conversation_id": "sid",
            "init": {"permission_mode": "always-proceed"},
        }
    )[0]
    assert isinstance(started, StartedEvent)
    assert (
        event(
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 1,
                    "state": "DONE",
                    "text_delta": "hello",
                },
            }
        )
        == []
    )
    action = {"step_type": "run_command", "step_index": 2, "state": "ACTIVE"}
    assert event({"event": "step_update", "step_update": action})[0].phase == "started"
    assert event({"event": "step_update", "step_update": action}) == []
    action["state"] = "DONE"
    assert (
        event({"event": "step_update", "step_update": action})[0].phase == "completed"
    )
    completed = event(
        {
            "event": "result",
            "result": {"status": status, "conversation_id": sid, "response": "hello"},
        }
    )[0]
    assert isinstance(completed, CompletedEvent) and completed.ok is ok
    assert completed.resume == started.resume
    assert event([]) == [] and event({"event": "future"}) == []
    assert event({"event": "init"}) == []


def test_agy_permission_guard_and_config_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = AiMemoryConfig("ai-memory", tmp_path / "state.db")
    config.allow_import("agy", "sid", tmp_path)
    launch = config.prepare(
        "agy",
        ["agy", "--conversation", "sid"],
        ResumeToken("agy", "sid"),
        tmp_path,
        None,
    )
    assert launch.config_home == str(tmp_path / ".gemini/antigravity-cli")
    with pytest.raises(RuntimeError, match="skip-permissions"):
        launch.bind(
            StartedEvent(
                "agy", ResumeToken("agy", "sid"), meta={"permission_mode": "ask"}
            )
        )
    launch.bind(
        StartedEvent(
            "agy", ResumeToken("agy", "sid"), meta={"permission_mode": "always-proceed"}
        )
    )
