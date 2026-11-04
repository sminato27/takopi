"""/cotas: each agent's last known 5h and weekly quota, without running one."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import anyio
from anyio.to_thread import run_sync

from ...logging import get_logger
from ...quota_status import (
    QUOTA_ENGINES,
    QuotaStore,
    QuotaView,
    render_quota_report,
)
from ...runners.codex_telemetry import latest_codex_quota
from ...telemetry import Telemetry
from ..types import TelegramIncomingMessage
from .reply import make_reply

if TYPE_CHECKING:
    from ..bridge import TelegramBridgeConfig

logger = get_logger(__name__)

_CODEX_TIMEOUT_S = 3
# Codex reads get their own worker, apart from the threads the runners use.
# A timed-out read keeps running and holds the lock, so later requests use the
# saved data instead of starting another one.
_CODEX_LIMITER = anyio.CapacityLimiter(1)
_CODEX_READING = threading.Lock()


def _read_codex() -> tuple[float, Telemetry] | None:
    if not _CODEX_READING.acquire(blocking=False):
        return None
    try:
        return latest_codex_quota()
    finally:
        _CODEX_READING.release()


async def _refresh_codex(store: QuotaStore) -> None:
    # Codex keeps its windows in local session logs, so no model call is needed.
    # A failed or slow read only leaves the saved snapshot in place.
    if _CODEX_READING.locked():
        logger.info("quota.codex_refresh_skipped")
        return
    try:
        with anyio.fail_after(_CODEX_TIMEOUT_S):
            found = await run_sync(
                _read_codex, abandon_on_cancel=True, limiter=_CODEX_LIMITER
            )
        if found is not None:
            observed_at, status = found
            store.merge_if_newer("codex", status, min(observed_at, time.time()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("quota.codex_refresh_failed", error_type=exc.__class__.__name__)


async def show_quotas(cfg: TelegramBridgeConfig, msg: TelegramIncomingMessage) -> None:
    reply = make_reply(cfg, msg)
    try:
        store = cfg.exec_cfg.quota_store
        if store is None:
            await reply(
                text="O registro de cotas não está disponível nesta instalação."
            )
            return
        configured = set(cfg.runtime.engine_ids)
        missing = set(cfg.runtime.missing_engine_ids())
        if "codex" in configured - missing:
            await _refresh_codex(store)
        now = time.time()
        views = [
            QuotaView(engine, note="não configurado")
            if engine not in configured
            else QuotaView(engine, note="CLI ausente")
            if engine in missing
            else store.view(engine, now)
            for engine in QUOTA_ENGINES
        ]
        await reply(text=render_quota_report(views, now))
    except Exception as exc:  # noqa: BLE001
        # A failed summary must not take down the message loop.
        logger.warning("quota.command_failed", error_type=exc.__class__.__name__)
        await reply(text="Não foi possível mostrar as cotas agora.")
