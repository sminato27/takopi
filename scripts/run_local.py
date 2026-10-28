"""Fail closed if the pilot's required routing or permission settings are missing."""

import os
from pathlib import Path
import sys
import tomllib


def main() -> None:
    path = Path.home() / ".takopi/takopi.toml"
    if not path.is_file():
        raise SystemExit(
            "Configure the dedicated bot with scripts/configure_local.py first."
        )
    config = tomllib.loads(path.read_text())
    if set(config.get("enabled_engines", [])) != {"codex", "claude"}:
        raise SystemExit("This pilot requires enabled_engines = ['codex', 'claude'].")
    telegram = config.get("transports", {}).get("telegram", {})
    owners = telegram.get("allowed_user_ids", [])
    if not owners or any(type(owner) is not int or owner <= 0 for owner in owners):
        raise SystemExit(
            "Set allowed_user_ids to your numeric Telegram user ID before starting."
        )
    if (
        config.get("codex", {}).get("ai_memory") is not True
        or config.get("claude", {}).get("ai_memory") is not True
    ):
        raise SystemExit("Both agents must use ai_memory = true.")
    if config.get("codex", {}).get("mode") != "exec":
        raise SystemExit("This pilot requires codex.mode = exec.")
    if config.get("claude", {}).get("permission_mode") != "auto":
        raise SystemExit("This pilot requires claude.permission_mode = auto.")
    if not telegram.get("topics", {}).get("session_topics"):
        raise SystemExit("Enable topics.session_topics for this pilot.")
    binary = Path(sys.executable).parent / "takopi"
    os.execv(binary, [str(binary)])


if __name__ == "__main__":
    main()
