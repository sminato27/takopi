"""Per-run status, kept separate from cumulative billing/token counters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import time
from typing import Any

from .model import Action, ActionEvent


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def token_count(usage: Any, keys: tuple[str, ...]) -> int | None:
    if not isinstance(usage, dict) or number(usage.get(keys[0])) is None:
        return None
    values = [number(usage.get(key, 0)) for key in keys]
    if any(value is None for value in values):
        return None
    return int(sum(value for value in values if value is not None))


@dataclass(frozen=True, slots=True)
class Quota:
    used_percent: float
    resets_at: float | None = None

    @classmethod
    def from_values(cls, used: Any, reset: Any) -> Quota | None:
        value = number(used)
        if value is None or value > 100:
            return None
        return cls(value, number(reset))


@dataclass(frozen=True, slots=True)
class Telemetry:
    model: str | None = None
    effort: str | None = None
    context_tokens: int | None = None
    context_window: int | None = None
    last_call_tokens: int | None = None
    five_hour: Quota | None = None
    weekly: Quota | None = None
    quota_group: str | None = None
    # The provider's own verdict; None when it reported none.
    limited: bool | None = None


def telemetry_event(engine: str, status: Telemetry) -> ActionEvent:
    return ActionEvent(
        engine=engine,
        action=Action(
            id="takopi.telemetry",
            kind="telemetry",
            title="session status",
            detail={"telemetry": status},
        ),
        phase="updated",
    )


def _text(value: str | None) -> str:
    # Keep engine-provided names inside one code span, with bounded size.
    return " ".join((value or "indisponível").replace("`", "").split())[:100]


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def duration(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    if minutes >= 1440:
        return f"{minutes // 1440}d {(minutes % 1440) // 60}h"
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}min"
    return f"{minutes}min"


def quota_line(label: str, quota: Quota | None, now: float) -> str:
    if quota is None:
        return f"{label}: indisponível"
    if quota.resets_at is not None and quota.resets_at <= now:
        return f"{label}: aguardando atualização"
    result = f"{label}: {quota.used_percent:.0f}% usado"
    if quota.resets_at is not None:
        result += f" · renova em {duration(quota.resets_at - now)}"
    return result


def telemetry_lines(status: Telemetry, *, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines = [f"Modelo: `{_text(status.model)}` · Effort: `{_text(status.effort)}`"]
    tokens, window = status.context_tokens, status.context_window
    if tokens is not None and window is not None and window > 0:
        context = (
            f"{100 * tokens / window:.0f}% usado ({_tokens(tokens)}/{_tokens(window)})"
        )
    elif tokens is not None:
        context = f"{_tokens(tokens)} tokens · capacidade indisponível"
    else:
        context = "indisponível"
        if status.last_call_tokens is not None:
            context += f" · última chamada: {_tokens(status.last_call_tokens)} tokens"
    lines.append(f"Contexto: {context}")
    group = f" ({_text(status.quota_group)})" if status.quota_group else ""
    lines.append(f"Cotas{group} · {quota_line('5h', status.five_hour, now)}")
    lines.append(quota_line("Semanal", status.weekly, now))
    return lines


def timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
