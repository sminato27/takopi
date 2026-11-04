"""Kiro CLI V3 headless runner, verified against kiro-cli 3.0.0.

Only runFinished/runError settles a request: ACP turn_end can precede more work.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import shutil
from typing import Any

from ..backends import EngineBackend, EngineConfig
from ..config import ConfigError
from ..events import EventFactory
from ..model import Action, ActionKind, ResumeToken, TakopiEvent
from ..runner import JsonlSubprocessRunner, ResumeTokenMixin, Runner
from ..schemas.kiro import KiroEvent, decode_event
from ..telemetry import Telemetry, number, telemetry_event
from .run_options import get_run_options

ENGINE = "kiro"
_SESSION = re.compile(r"[a-zA-Z0-9_-]{1,200}\Z")
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_KINDS: dict[str, ActionKind] = {
    "execute": "command",
    "read": "tool",
    "edit": "file_change",
    "delete": "file_change",
    "search": "tool",
    "think": "note",
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(slots=True)
class KiroState:
    factory: EventFactory = field(default_factory=lambda: EventFactory(ENGINE))
    telemetry: Telemetry = field(default_factory=Telemetry)
    tools: dict[str, Action] = field(default_factory=dict)
    parts: list[str] = field(default_factory=list)
    message_id: str | None = None
    note_seq: int = 0


@dataclass(slots=True)
class KiroRunner(ResumeTokenMixin, JsonlSubprocessRunner):
    engine: str = ENGINE
    defer_completion = True
    resume_re: re.Pattern[str] = re.compile(
        r"(?im)^\s*`?kiro-cli\s+chat\s+--resume-id\s+(?P<token>[a-zA-Z0-9_-]{1,200})`?\s*$"
    )
    kiro_cmd: str = "kiro-cli"
    model: str | None = None
    effort: str | None = None
    agent: str | None = None
    trust_all_tools: bool = False

    def command(self) -> str:
        return self.kiro_cmd

    def format_resume(self, token: ResumeToken) -> str:
        if token.engine != self.engine or not _SESSION.fullmatch(token.value):
            raise RuntimeError("invalid Kiro resume token")
        return f"`kiro-cli chat --resume-id {token.value}`"

    def new_state(self, prompt: str, resume: ResumeToken | None) -> KiroState:
        options = get_run_options()
        return KiroState(
            telemetry=Telemetry(
                model=(options.model if options else None) or self.model,
                effort=(options.reasoning if options else None) or self.effort,
            )
        )

    def build_args(
        self, prompt: str, resume: ResumeToken | None, *, state: Any
    ) -> list[str]:
        # Pin V3: Kiro otherwise changes its harness depending on trust flags.
        # stdin carries the prompt, so text beginning with '-' is never an option.
        args = [
            "chat",
            "--v3",
            "--no-interactive",
            "--trust-workspace",
            "--output-format",
            "stream-json",
        ]
        if resume:
            self.format_resume(resume)
            args.extend(["--resume-id", resume.value])
        if self.trust_all_tools:
            args.append("--trust-all-tools")
        options = get_run_options()
        model = (options.model if options else None) or self.model
        effort = (options.reasoning if options else None) or self.effort
        agent = self.agent
        if agent is None and model is not None and model.startswith("claude"):
            # No agent configured: an Anthropic model gets the ported Claude
            # Code subagent pipeline (search/grep/docs/.../consultant) and its
            # efficiency rules, registered globally as ~/.kiro/agents/claude-pipeline.json.
            # A non-Claude model keeps Kiro's own default agent.
            agent = "claude-pipeline"
        if agent:
            args.extend(["--agent", agent])
        if model:
            args.extend(["--model", model])
        if effort:
            if effort not in EFFORTS:
                raise ConfigError(f"Invalid Kiro effort: {effort}")
            args.extend(["--effort", effort])
        return args

    def decode_jsonl(self, *, line: bytes) -> KiroEvent:
        return decode_event(line)

    def translate(
        self,
        data: KiroEvent,
        *,
        state: KiroState,
        resume: ResumeToken | None,
        found_session: ResumeToken | None,
    ) -> list[TakopiEvent]:
        payload = data.data
        events: list[TakopiEvent] = []
        if data.type not in {"sessionUpdate", "runFinished", "runError"}:
            return events
        sid = payload.get("sessionId")
        token = (
            ResumeToken(ENGINE, sid)
            if isinstance(sid, str) and _SESSION.fullmatch(sid)
            else None
        )
        expected = state.factory.resume or resume
        if token is not None and expected is not None and token != expected:
            raise RuntimeError("Kiro emitted an event for another session")
        if token is not None and state.factory.resume is None:
            events.append(
                state.factory.started(
                    token, title="Kiro", meta={"telemetry": state.telemetry}
                )
            )
        if data.type == "sessionUpdate":
            update = _dict(payload.get("update"))
            meta = _dict(_dict(update.get("_meta")).get("kiro"))
            # History can be replayed during load; it is not this turn's output.
            if meta.get("replay") is True:
                return events
            if token is None:
                return events
            events.extend(self._update(update, meta, state))
            return events
        if data.type == "runError":
            message = payload.get("message")
            error = (
                message
                if isinstance(message, str) and message.strip()
                else "Kiro terminou com erro."
            )
            events.append(
                state.factory.completed_error(
                    error=error,
                    answer="".join(state.parts),
                    resume=expected or token,
                    usage={"telemetry": state.telemetry},
                )
            )
            return events
        final = payload.get("finalText")
        answer = final if isinstance(final, str) else ""
        streamed = "".join(state.parts)
        if payload.get("finalTextTruncated") is True and streamed.startswith(answer):
            answer = streamed
        ok = (
            payload.get("status") == "success"
            and token is not None
            and bool(answer.strip())
        )
        error = (
            None
            if ok
            else f"Kiro: {payload.get('message') or payload.get('stopReason') or payload.get('status') or 'resposta ausente'}"
        )
        if not answer.strip() and payload.get("status") == "success":
            error = (
                "Kiro terminou sem uma resposta. Confira a sessão antes de reenviar."
            )
        events.append(
            state.factory.completed(
                ok=ok,
                answer=answer,
                error=error,
                resume=expected or token,
                usage={"telemetry": state.telemetry},
            )
        )
        return events

    def translate_error_events(
        self, *, data: Any, error: Exception, state: Any
    ) -> list[TakopiEvent]:
        # Session mismatches must stop the process, never blend two histories.
        if isinstance(error, RuntimeError):
            raise error
        return super().translate_error_events(data=data, error=error, state=state)

    def _update(self, update: dict, meta: dict, state: KiroState) -> list[TakopiEvent]:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = _dict(update.get("content"))
            text = content.get("text")
            message_id = meta.get("replayId")
            if isinstance(message_id, str) and message_id != state.message_id:
                state.parts.clear()
                state.message_id = message_id
            if content.get("type") == "text" and isinstance(text, str):
                state.parts.append(text)
        elif kind in {"tool_call", "tool_call_update"}:
            tool_id = update.get("toolCallId")
            if not isinstance(tool_id, str) or not tool_id:
                return []
            previous = state.tools.get(tool_id)
            title = update.get("title")
            action = Action(
                id=tool_id,
                kind=previous.kind
                if previous
                else _KINDS.get(str(update.get("kind") or ""), "tool"),
                title=title
                if isinstance(title, str)
                else previous.title
                if previous
                else "Kiro tool",
                detail={},
            )
            state.tools[tool_id] = action
            status = update.get("status")
            if status in {"completed", "failed"}:
                return [
                    state.factory.action_completed(
                        action_id=tool_id,
                        kind=action.kind,
                        title=action.title,
                        ok=status == "completed",
                    )
                ]
            method = (
                state.factory.action_updated
                if previous
                else state.factory.action_started
            )
            return [method(action_id=tool_id, kind=action.kind, title=action.title)]
        elif kind == "config_option_update":
            options = update.get("configOptions")
            if not isinstance(options, list):
                return []
            state.telemetry = replace(state.telemetry, effort=None)
            for option in options:
                if not isinstance(option, dict):
                    continue
                value = option.get("currentValue")
                if not isinstance(value, str):
                    continue
                if option.get("id") == "model":
                    state.telemetry = replace(state.telemetry, model=value)
                elif option.get("id") == "effortLevel":
                    state.telemetry = replace(state.telemetry, effort=value)
            return [telemetry_event(ENGINE, state.telemetry)]
        elif kind == "session_info_update" and meta.get("kind") == "context_usage":
            percent = number(meta.get("usagePercentage"))
            if percent is not None and percent <= 100:
                state.telemetry = replace(state.telemetry, context_percent=percent)
                return [telemetry_event(ENGINE, state.telemetry)]
        return []


def build_runner(config: EngineConfig, config_path: Path) -> Runner:
    if config.get("ai_memory"):
        raise ConfigError(
            "The Kiro adapter runs directly; ai_memory is not supported for Kiro."
        )
    for key in ("model", "effort", "agent"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"kiro.{key} must be a non-empty string")
    trust = config.get("trust_all_tools", False)
    if not isinstance(trust, bool):
        raise ConfigError("kiro.trust_all_tools must be true or false")
    if config.get("effort") is not None and config["effort"] not in EFFORTS:
        raise ConfigError("Invalid kiro.effort")
    return KiroRunner(
        kiro_cmd=shutil.which("kiro-cli") or "kiro-cli",
        model=config.get("model"),
        effort=config.get("effort"),
        agent=config.get("agent"),
        trust_all_tools=trust,
    )


BACKEND = EngineBackend(
    id=ENGINE,
    build_runner=build_runner,
    cli_cmd="kiro-cli",
    install_cmd="See https://kiro.dev/docs/cli/installation/",
)
