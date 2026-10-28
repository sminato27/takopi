"""Exercise setup against a mock Telegram API, including its failure boundaries."""

import json
from pathlib import Path
import runpy
import sys
import tomllib

import httpx
import pytest

from takopi.settings import load_settings


@pytest.fixture
def setup_bot(tmp_path, monkeypatch):
    token = "12345:FAKE_LOCAL_TEST_TOKEN"
    token_file = tmp_path / "token"
    token_file.write_text(token)
    responses = {
        "getMe": {"id": 12345, "username": "pilot_test_bot"},
        "getWebhookInfo": {"url": ""},
        "getChat": {"type": "supergroup", "is_forum": True},
    }

    def request_handler(request):
        method = request.url.path.rsplit("/", 1)[1]
        if method == "getChatMember":
            payload = json.loads(request.content)
            result = (
                {"status": "administrator", "can_manage_topics": True}
                if payload["user_id"] == 12345
                else {"status": "member", "user": {"is_bot": False}}
            )
        else:
            result = responses[method]
        return httpx.Response(200, json={"ok": True, "result": result})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(request_handler), **kwargs
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_local.py",
            "--token-file",
            str(token_file),
            "--chat-id",
            "-100123",
            "--owner-id",
            "42",
        ],
    )
    script = Path(__file__).parents[1] / "scripts/configure_local.py"
    main = runpy.run_path(str(script))["main"]
    return main, tmp_path / ".takopi/takopi.toml", responses, token


def test_configured_pilot_loads_and_preserves_existing_config(setup_bot, capsys):
    main, path, responses, token = setup_bot
    main()
    assert token not in capsys.readouterr().out
    assert path.stat().st_mode & 0o777 == 0o600
    settings, _ = load_settings(path)
    assert settings.transports.telegram is not None
    assert settings.transports.telegram.allowed_user_ids == [42]
    assert settings.transports.telegram.topics.session_topics
    assert settings.plugins.enabled == ["codex", "claude", "telegram"]
    raw = tomllib.loads(path.read_text())
    assert raw["codex"]["ai_memory"] and raw["codex"]["mode"] == "exec"
    assert raw["claude"]["ai_memory"] and raw["claude"]["permission_mode"] == "auto"
    original = path.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert path.read_bytes() == original
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err


@pytest.mark.parametrize(
    ("method", "response"),
    [
        ("getWebhookInfo", {"url": "https://existing.example/webhook"}),
        ("getChat", {"type": "group", "is_forum": False}),
    ],
)
def test_invalid_bot_or_group_does_not_write_config(setup_bot, method, response):
    main, path, responses, token = setup_bot
    responses[method] = response
    with pytest.raises(SystemExit):
        main()
    assert not path.exists()
