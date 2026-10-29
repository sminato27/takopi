"""Antigravity's read-only, zero-token print commands for model and quotas."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

import anyio

from ..telemetry import Quota, Telemetry, number, timestamp
from ..utils.paths import get_run_base_dir


async def _query(
    executable: str,
    command: str,
    session: str,
    model: str | None,
    effort: str | None,
) -> dict[str, Any]:
    args = [executable, "--conversation", session, "--output-format", "json"]
    if model:
        args.extend(["--model", model])
    if effort:
        args.extend(["--effort", effort])
    args.extend(["-p", f"/{command}"])
    try:
        with anyio.fail_after(6):
            result = await anyio.run_process(args, cwd=get_run_base_dir(), check=False)
        if result.returncode or len(result.stdout) > 256_000:
            return {}
        data = json.loads(result.stdout)
        report = data.get("command", {})
        if report.get("name") == command and isinstance(report.get("data"), dict):
            return report["data"]
    except (OSError, TimeoutError, ValueError, AttributeError, TypeError):
        return {}
    return {}


def apply_reports(status: Telemetry, model: dict, usage: dict) -> Telemetry:
    label, effort = model.get("label"), model.get("effort")
    status = replace(
        status,
        model=label if isinstance(label, str) else status.model,
        effort=effort if isinstance(effort, str) else status.effort,
    )
    # Match only known families. An unrecognized model must not inherit another
    # group's remaining quota. Both values come from this same refresh.
    model_id = model.get("id")
    if not isinstance(model_id, str):
        return status
    if model_id.startswith("gemini-"):
        bucket_prefix, label = "gemini-", "Gemini"
    elif model_id.startswith(("claude-", "gpt-")):
        bucket_prefix, label = "3p-", "Claude/GPT"
    else:
        return status
    groups = usage.get("groups")
    if not isinstance(groups, list):
        return status
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        for bucket in group["buckets"]:
            if not isinstance(bucket, dict):
                continue
            bucket_id = bucket.get("id")
            if not isinstance(bucket_id, str):
                continue
            field = {
                bucket_prefix + "5h": "five_hour",
                bucket_prefix + "weekly": "weekly",
            }.get(bucket_id)
            fraction = number(bucket.get("remaining_fraction"))
            if field and fraction is not None and fraction <= 1:
                quota = Quota.from_values(
                    100 * (1 - fraction), timestamp(bucket.get("reset_time"))
                )
                status = replace(status, **{field: quota, "quota_group": label})
    return status


async def refresh_agy(
    status: Telemetry,
    *,
    executable: str,
    session: str,
    model: str | None = None,
    effort: str | None = None,
) -> Telemetry:
    reports: dict[str, dict] = {}

    async def read(command: str) -> None:
        reports[command] = await _query(executable, command, session, model, effort)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(read, "model")
        tasks.start_soon(read, "usage")
    return apply_reports(status, reports["model"], reports["usage"])
