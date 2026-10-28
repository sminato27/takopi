"""Configure the local Telegram pilot without placing a bot token in shell history."""

import argparse
import os
from pathlib import Path
import re

import httpx
import tomli_w


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--discover",
        action="store_true",
        help="List group/user IDs from recent bot updates",
    )
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--owner-id", type=int)
    parser.add_argument("--project", action="append", default=[], metavar="ALIAS=PATH")
    args = parser.parse_args()
    token = args.token_file.expanduser().read_text().strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        parser.error("The token file does not contain a Telegram bot token")

    with httpx.Client(timeout=15) as client:

        def api(method: str, **payload):
            try:
                response = client.post(
                    f"https://api.telegram.org/bot{token}/{method}", json=payload
                )
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise SystemExit(
                    f"Telegram request failed: {method} (token omitted)"
                ) from None
            if not data.get("ok"):
                raise SystemExit(
                    f"Telegram rejected {method}: HTTP {response.status_code} (token omitted)"
                )
            return data["result"]

        bot = api("getMe")
        if api("getWebhookInfo").get("url"):
            parser.error(
                "This bot already has a webhook. Use a dedicated bot for this pilot."
            )
        if args.discover:
            print(f"Bot: @{bot['username']}")
            seen = set()
            for update in api("getUpdates", timeout=0, allowed_updates=["message"]):
                message = update.get("message", {})
                chat, sender = message.get("chat", {}), message.get("from", {})
                key = (chat.get("id"), sender.get("id"))
                if key in seen or not all(key):
                    continue
                seen.add(key)
                print(
                    f"chat_id={key[0]} owner_candidate={key[1]} "
                    f"chat={chat.get('title', chat.get('type', ''))!r} "
                    f"user={sender.get('username', sender.get('first_name', ''))!r}"
                )
            print(
                "If no group appears, send /setup in the group and run --discover again."
            )
            return
        if (
            not args.chat_id
            or args.chat_id >= 0
            or not args.owner_id
            or args.owner_id <= 0
        ):
            parser.error(
                "--chat-id must be a group ID and --owner-id must be your numeric user ID"
            )
        chat = api("getChat", chat_id=args.chat_id)
        if chat.get("type") != "supergroup" or not chat.get("is_forum"):
            parser.error("The selected group must have topics enabled")
        member = api("getChatMember", chat_id=args.chat_id, user_id=bot["id"])
        if member.get("status") != "administrator" or not member.get(
            "can_manage_topics"
        ):
            parser.error("The bot needs administrator status and Manage Topics")
        owner = api("getChatMember", chat_id=args.chat_id, user_id=args.owner_id)
        if owner.get("status") in {"left", "kicked"} or owner.get("user", {}).get(
            "is_bot"
        ):
            parser.error("The owner must be a human member of the selected group")

    test_project = Path.home() / ".local/share/takopi-test-project"
    projects = {"teste": {"path": str(test_project)}}
    for spec in args.project:
        alias, separator, value = spec.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", alias):
            parser.error(
                "Use --project alias=/absolute/path; aliases use lowercase letters, numbers and underscores"
            )
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"Project directory does not exist: {path}")
        if alias == "teste":
            parser.error("teste is reserved for the integration test project")
        projects[alias] = {"path": str(path)}

    config = {
        "default_engine": "codex",
        "enabled_engines": ["codex", "claude"],
        "default_project": "teste",
        "transport": "telegram",
        "watch_config": True,
        "plugins": {"enabled": ["takopi"]},
        "codex": {"mode": "exec", "ai_memory": True, "extra_args": []},
        "claude": {"ai_memory": True, "permission_mode": "auto"},
        "projects": projects,
        "transports": {
            "telegram": {
                "bot_token": token,
                "chat_id": args.chat_id,
                "allowed_user_ids": [args.owner_id],
                "session_mode": "chat",
                "message_overflow": "split",
                "show_resume_line": False,
                "voice_transcription": False,
                "topics": {
                    "enabled": True,
                    "scope": "main",
                    "session_topics": True,
                    "import_native_sessions": True,
                },
                "files": {"enabled": False},
            }
        },
    }
    config_dir = Path.home() / ".takopi"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    path = config_dir / "takopi.toml"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error(
            f"{path} already exists; it was preserved. Edit it explicitly to change settings."
        )
    with os.fdopen(fd, "wb") as output:
        tomli_w.dump(config, output)
    test_project.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(
        f"Configured @{bot['username']} for group {args.chat_id}, owner {args.owner_id}."
    )
    print(f"Configuration saved privately to {path}; token omitted.")
    print("Start with: systemctl --user start takopi")


if __name__ == "__main__":
    main()
