"""Last known 5h and weekly quota of each agent, kept between runs for /cotas.

Only percentages, reset and observation times and limit flags are saved: no
prompts, error text, session ids, plans or credits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import time
from typing import Any

import msgspec

from .logging import get_logger
from .markdown import HARD_BREAK
from .model import CompletedEvent
from .telemetry import Quota, Telemetry, duration, quota_line
from .utils.json_state import atomic_write_json

logger = get_logger(__name__)

STATE_VERSION = 1
STATE_FILENAME = "quota_status_state.json"

QUOTA_ENGINES = ("claude", "codex", "agy", "kiro")
# Agy's model families, as labelled by runners/agy_telemetry.py; the others
# have one quota. An agent is out only when all of its groups are.
QUOTA_GROUPS = {"claude": ("",), "codex": ("",), "agy": ("Gemini", "Claude/GPT")}
AVAILABLE = "DISPONÍVEL"
UNAVAILABLE = "INDISPONÍVEL"
NO_DATA = "SEM DADOS"

_TITLES = {"claude": "Claude", "codex": "Codex", "agy": "Agy", "kiro": "Kiro"}
# Agy's failure text is not a reliable quota signal; its windows are.
_ERROR_TEXT_ENGINES = frozenset({"claude", "codex"})
_FIVE_HOURS = 5 * 3600
_WINDOWS = (("five_hour", "5h", _FIVE_HOURS), ("weekly", "Semanal", 7 * 86400))
_STALE_AFTER = 24 * 3600
# Bare "limit reached" would also match Claude's "Context limit reached".
_USAGE_LIMIT = re.compile(
    r"usage limit|hit your (?:(?:usage|5-hour|weekly|session|opus|sonnet) )?limit"
    r"|usage_limit_reached"
    r"|(?:5-hour|weekly|session|opus|sonnet) limit reached|limite de uso",
    re.IGNORECASE,
)


class _Window(msgspec.Struct, forbid_unknown_fields=False):
    used_percent: float
    resets_at: float | None = None
    observed_at: float | None = None


class _Group(msgspec.Struct, forbid_unknown_fields=False):
    five_hour: _Window | None = None
    weekly: _Window | None = None
    observed_at: float | None = None
    limited: bool = False
    limited_at: float | None = None
    limited_until: float | None = None


class _State(msgspec.Struct, forbid_unknown_fields=False):
    version: int
    engines: dict[str, dict[str, _Group]] = msgspec.field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    label: str
    until: float | None = None
    exhausted: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroupView:
    label: str
    five_hour: Quota | None
    weekly: Quota | None
    observed_at: float | None
    limited_at: float | None
    five_hour_observed_at: float | None = None
    weekly_observed_at: float | None = None


@dataclass(frozen=True, slots=True)
class QuotaView:
    engine: str
    status: QuotaStatus | None = None
    groups: tuple[GroupView, ...] = ()
    # Why the bot cannot run this engine; replaces the quota lines.
    note: str | None = None


def resolve_quota_path(config_path: Path) -> Path:
    return config_path.with_name(STATE_FILENAME)


def detect_usage_limit(text: str | None) -> bool:
    return text is not None and _USAGE_LIMIT.search(text) is not None


def _has_data(group: _Group) -> bool:
    return group.limited or group.five_hour is not None or group.weekly is not None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _clean_state(raw: Any) -> Any:
    # A hand-edited file may hold values the store never writes. Drop windows
    # without finite numbers, clamp usage to 0..100 and forget invalid times.
    engines = raw.get("engines") if isinstance(raw, dict) else None
    for groups in engines.values() if isinstance(engines, dict) else ():
        for group in groups.values() if isinstance(groups, dict) else ():
            if not isinstance(group, dict):
                continue
            for name, _, _ in _WINDOWS:
                window = group.get(name)
                if window is None:
                    continue
                used = (
                    _finite(window.get("used_percent"))
                    if isinstance(window, dict)
                    else None
                )
                reset = window.get("resets_at") if isinstance(window, dict) else None
                if used is not None and (reset is None or _finite(reset) is not None):
                    seen = _finite(window.get("observed_at"))
                    group[name] = {
                        **window,
                        "used_percent": min(100.0, max(0.0, used)),
                        # Older snapshots timestamped both windows together.
                        "observed_at": seen
                        if seen is not None
                        else _finite(group.get("observed_at")),
                    }
                else:
                    group[name] = None
            for name in ("observed_at", "limited_at", "limited_until"):
                if group.get(name) is not None and _finite(group[name]) is None:
                    group[name] = None
    return raw


def _window_end(
    resets_at: float | None, observed_at: float | None, seconds: int
) -> float | None:
    # Without a reset time, a window ends at most its length after it was seen.
    if resets_at is not None:
        return resets_at
    return None if observed_at is None else observed_at + seconds


def _blocked_until(group: _Group, now: float) -> float | None:
    ends: list[float] = []
    for name, _, seconds in _WINDOWS:
        window: _Window | None = getattr(group, name)
        if window is None or window.used_percent < 100:
            continue
        end = _window_end(window.resets_at, window.observed_at, seconds)
        if end is not None and end > now:
            ends.append(end)
    until = group.limited_until
    if group.limited and until is not None and until > now:
        ends.append(until)
    return max(ends, default=None)


def _limited_until(group: _Group, now: float) -> float:
    # Never None, so a limit always expires. Exhausted windows hold until their
    # reset; otherwise prefer nearly used ones, then any known reset.
    resets: list[tuple[float, float]] = []
    for window in (group.five_hour, group.weekly):
        reset = window.resets_at if window is not None else None
        if window is not None and reset is not None and reset > now:
            resets.append((window.used_percent, reset))
    for threshold in (100, 90):
        candidates = [reset for used, reset in resets if used >= threshold]
        if candidates:
            return max(candidates)
    if resets:
        return min(reset for _, reset in resets)
    return now + _FIVE_HOURS


def _set_limited(group: _Group, limited: bool, now: float) -> None:
    if limited:
        group.limited, group.limited_at = True, now
        group.limited_until = _limited_until(group, now)
    else:
        group.limited, group.limited_at, group.limited_until = False, None, None


def _quota(window: _Window | None) -> Quota | None:
    return None if window is None else Quota(window.used_percent, window.resets_at)


def _group_key(engine: str, telemetry: Telemetry) -> str | None:
    # Agy keeps one entry per model family; an unknown family has none.
    return telemetry.quota_group if engine == "agy" else ""


class QuotaStore:
    """The in-memory state is authoritative; the file is rewritten on change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # Set while a change exists only in memory, e.g. after a full disk.
        self._dirty = False
        self._state = self._load()

    def _load(self) -> _State:
        try:
            # Python's parser keeps NaN and 1e999 so that only the affected
            # windows are dropped, not the whole file.
            raw = _clean_state(json.loads(self._path.read_bytes()))
            state = msgspec.convert(raw, type=_State)
        except FileNotFoundError:
            return _State(version=STATE_VERSION)
        except (OSError, ValueError, RecursionError, msgspec.MsgspecError) as exc:
            logger.warning(
                "quota.state.load_failed",
                path=str(self._path),
                error_type=exc.__class__.__name__,
            )
            return _State(version=STATE_VERSION)
        if state.version != STATE_VERSION:
            logger.warning(
                "quota.state.version_mismatch",
                path=str(self._path),
                version=state.version,
                expected=STATE_VERSION,
            )
            return _State(version=STATE_VERSION)
        return state

    def _save(self) -> None:
        try:
            atomic_write_json(self._path, msgspec.to_builtins(self._state))
        except Exception as exc:  # noqa: BLE001
            # Never fail a run over this file; the next call retries.
            self._dirty = True
            logger.warning("quota.state.save_failed", error_type=exc.__class__.__name__)
            return
        self._dirty = False

    def observe(
        self,
        engine: str,
        telemetry: Telemetry,
        completed: CompletedEvent | None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        if self._update(engine, telemetry, completed, now) or self._dirty:
            self._save()

    def _update(
        self,
        engine: str,
        telemetry: Telemetry,
        completed: CompletedEvent | None,
        now: float,
    ) -> bool:
        key = _group_key(engine, telemetry)
        if engine not in QUOTA_ENGINES or key is None:
            return False
        current = self._state.engines.get(engine, {}).get(key)
        group = _Group() if current is None else msgspec.structs.replace(current)
        reported: set[str] = set()
        for name, _, _ in _WINDOWS:
            quota: Quota | None = getattr(telemetry, name)
            # Missing values keep the previous ones.
            if quota is not None:
                setattr(group, name, _Window(quota.used_percent, quota.resets_at, now))
                reported.add(name)
        if reported:
            group.observed_at = now
        if telemetry.limited is not None:
            _set_limited(group, telemetry.limited, now)
        # The run's result has the last word. Other failures, such as a 429 or
        # a network error, say nothing about the quota.
        if completed is not None:
            if completed.ok:
                _set_limited(group, False, now)
                # A success proves the agent usable: an exhausted window that
                # this run did not report again is outdated.
                for name, _, _ in _WINDOWS:
                    window: _Window | None = getattr(group, name)
                    if name not in reported and window and window.used_percent >= 100:
                        setattr(group, name, None)
            elif engine in _ERROR_TEXT_ENGINES and detect_usage_limit(completed.error):
                _set_limited(group, True, now)
        if group == current or (current is None and not _has_data(group)):
            return False
        self._state.engines.setdefault(engine, {})[key] = group
        return True

    def merge_if_newer(
        self, engine: str, telemetry: Telemetry, observed_at: float
    ) -> bool:
        # Data read outside a run must not undo a later observation.
        key = _group_key(engine, telemetry)
        current = None if key is None else self._state.engines.get(engine, {}).get(key)
        seen = (
            []
            if current is None
            else [t for t in (current.observed_at, current.limited_at) if t is not None]
        )
        if key is None or (seen and max(seen) >= observed_at):
            if self._dirty:
                self._save()
            return False
        self.observe(engine, telemetry, None, now=observed_at)
        return True

    def status_for(self, engine: str, now: float) -> QuotaStatus:
        groups = {
            key: group
            for key, group in self._state.engines.get(engine, {}).items()
            if _has_data(group)
        }
        if not groups:
            return QuotaStatus(NO_DATA)
        blocked: dict[str, float] = {}
        for key, group in groups.items():
            until = _blocked_until(group, now)
            if until is not None:
                blocked[key] = until
        # Another model family may still work, even one without data yet.
        expected = QUOTA_GROUPS.get(engine, ("",))
        if all(key in blocked for key in expected) and len(blocked) == len(groups):
            return QuotaStatus(UNAVAILABLE, until=min(blocked.values()))
        return QuotaStatus(AVAILABLE, exhausted=tuple(sorted(blocked)))

    def view(self, engine: str, now: float) -> QuotaView:
        groups = tuple(
            GroupView(
                label=key,
                five_hour=_quota(group.five_hour),
                weekly=_quota(group.weekly),
                observed_at=group.observed_at,
                limited_at=group.limited_at if group.limited else None,
                five_hour_observed_at=group.five_hour.observed_at
                if group.five_hour is not None
                else None,
                weekly_observed_at=group.weekly.observed_at
                if group.weekly is not None
                else None,
            )
            for key, group in sorted(self._state.engines.get(engine, {}).items())
            if _has_data(group)
        )
        return QuotaView(engine, status=self.status_for(engine, now), groups=groups)


def _window_text(
    label: str,
    quota: Quota | None,
    observed_at: float | None,
    seconds: int,
    now: float,
) -> str:
    if quota is not None:
        end = _window_end(quota.resets_at, observed_at, seconds)
        if end is None or end <= now:
            return f"{label}: renovada (aguardando atualização)"
    return quota_line(label, quota, now)


def _group_lines(group: GroupView, now: float) -> list[str]:
    windows = [
        _window_text(
            label,
            getattr(group, name),
            getattr(group, f"{name}_observed_at"),
            seconds,
            now,
        )
        for name, label, seconds in _WINDOWS
    ]
    seen = group.observed_at if group.observed_at is not None else group.limited_at
    age = None
    if seen is not None:
        age = f"dado de há {duration(now - seen)}"
        if now - seen > _STALE_AFTER:
            age += " · pode estar desatualizado"
    if group.label:
        line = f"{group.label} · {' · '.join(windows)}"
        return [f"{line} ({age})" if age else line]
    return [*windows, f"({age})"] if age else windows


def _engine_lines(view: QuotaView, now: float) -> list[str]:
    title = f"**{_TITLES.get(view.engine, view.engine)}**"
    status = view.status
    if view.note is not None or status is None:
        return [f"⚫ {title} — {view.note or 'indisponível'}"]
    if status.label == UNAVAILABLE:
        back = (
            ""
            if status.until is None
            else f", volta em ~{duration(status.until - now)}"
        )
        head = f"🔴 {title} — {UNAVAILABLE} (limite atingido{back})"
    elif status.exhausted:
        names = ", ".join(status.exhausted)
        head = f"🟡 {title} — {AVAILABLE} (parcial: {names} esgotado)"
    elif status.label == NO_DATA:
        head = f"⚪ {title} — {NO_DATA}"
    else:
        head = f"🟢 {title} — {AVAILABLE}"
    lines = [head]
    for group in view.groups:
        lines.extend(_group_lines(group, now))
    if status.label == NO_DATA:
        lines.append(
            "O Kiro não informa janelas de 5h/semanal neste modo."
            if view.engine == "kiro"
            else "Nenhuma resposta pelo bot registrou cotas ainda."
        )
    return lines


def render_quota_report(views: Sequence[QuotaView], now: float) -> str:
    blocks = ["**Cotas dos agentes**"]
    blocks.extend(HARD_BREAK.join(_engine_lines(view, now)) for view in views)
    blocks.append(
        "Percentuais indicam uso. Claude e Agy mostram o último dado salvo de uma "
        "resposta pelo bot; nenhum agente é executado só para medir. O Codex também "
        "é lido dos registros locais."
    )
    return "\n\n".join(blocks)
