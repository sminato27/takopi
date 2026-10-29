"""Antigravity CLI stream-json protocol (verified with agy 1.4.9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shutil
from typing import Any

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..managed import AiMemoryConfig
from ..model import ResumeToken, TakopiEvent
from ..runner import JsonlSubprocessRunner, ResumeTokenMixin, Runner
from .run_options import get_run_options


@dataclass(slots=True)
class AgyState:
    factory: EventFactory = field(default_factory=lambda: EventFactory("agy"))
    note_seq: int = 0
    steps: set[int] = field(default_factory=set)


@dataclass(slots=True)
class AgyRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: str = "agy"
    resume_re: re.Pattern[str] = re.compile(
        r"(?im)^\s*`?agy\s+--conversation\s+(?P<token>[^`\s]+)`?\s*$"
    )
    agy_cmd: str = "agy"
    model: str | None = None
    ai_memory: AiMemoryConfig | None = None

    def command(self) -> str:
        return self.agy_cmd

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != self.engine:
            raise RuntimeError(f"resume token is for engine {token.engine!r}")
        return f"`agy --conversation {token.value}`"

    def new_state(self, prompt: str, resume: ResumeToken | None) -> AgyState:
        return AgyState()

    def build_args(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> list[str]:
        args = ["--dangerously-skip-permissions", "--output-format", "stream-json"]
        if resume is not None:
            args.extend(["--conversation", resume.value])
        options = get_run_options()
        model = (options.model if options else None) or self.model
        if model:
            args.extend(["--model", model])
        if options and options.reasoning:
            args.extend(["--effort", options.reasoning])
        # Unlike Claude, agy's -p consumes the prompt as its flag value.
        args.extend(["-p", prompt])
        return args

    def stdin_payload(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> None:
        return None

    def translate(
        self,
        data: Any,
        *,
        state: AgyState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[TakopiEvent]:
        if not isinstance(data, dict):
            return []
        event = data.get("event")
        if event == "init":
            sid = data.get("conversation_id")
            if not isinstance(sid, str) or not sid:
                return []
            meta = data.get("init", {})
            return [
                state.factory.started(
                    ResumeToken("agy", sid), title="Antigravity", meta=meta
                )
            ]
        if event == "step_update":
            step = data.get("step_update", {})
            kind, index = step.get("step_type"), step.get("step_index")
            if kind in {
                "user_input",
                "system_message",
                "agent_response",
            } or not isinstance(index, int):
                return []
            detail = {"step_type": kind}
            action_id = f"agy.step.{index}"
            title = str(kind or "Antigravity")
            if step.get("state") == "DONE":
                return [
                    state.factory.action_completed(
                        action_id=action_id,
                        kind="tool",
                        title=title,
                        ok=True,
                        detail=detail,
                    )
                ]
            if index not in state.steps:
                state.steps.add(index)
                return [
                    state.factory.action_started(
                        action_id=action_id, kind="tool", title=title, detail=detail
                    )
                ]
            return []
        if event == "result":
            result = data.get("result", {})
            sid = result.get("conversation_id")
            token = ResumeToken("agy", sid) if isinstance(sid, str) and sid else None
            expected = state.factory.resume or resume
            status = result.get("status")
            ok = status == "SUCCESS" and expected is not None and token == expected
            error = (
                None
                if ok
                else str(
                    result.get("error")
                    or f"Antigravity: {status or 'missing status/session'}"
                )
            )
            return [
                state.factory.completed(
                    ok=ok,
                    answer=str(result.get("response") or ""),
                    resume=expected,
                    error=error,
                    usage=result.get("usage"),
                )
            ]
        return []


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    if config.get("dangerously_skip_permissions") is not True:
        raise ConfigError(
            "agy requires dangerously_skip_permissions = true for this unattended adapter"
        )
    return AgyRunner(
        agy_cmd=shutil.which("agy") or "agy",
        model=config.get("model"),
        ai_memory=AiMemoryConfig.from_config(config, config_path),
    )


BACKEND = EngineBackend(id="agy", build_runner=build_runner, cli_cmd="agy")
