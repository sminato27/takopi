from dataclasses import replace
from pathlib import Path
import re

import pytest

from takopi.config import ProjectConfig, ProjectsConfig
from takopi.router import AutoRouter, RunnerEntry
from takopi.runners.mock import Return, ScriptRunner
from takopi.settings import TelegramFilesSettings, TelegramTopicsSettings
from takopi.telegram.commands.help import build_help_messages
from takopi.telegram.commands.menu import build_bot_commands
from takopi.telegram.loop import run_main_loop
from takopi.telegram.types import TelegramIncomingMessage
from takopi.transport_runtime import TransportRuntime
from tests.telegram_fakes import FakeTransport, make_cfg


def help_config(tmp_path, count=3):
    transport = FakeTransport()
    runners = [
        ScriptRunner([Return(answer="unexpected")], engine=engine)
        for engine in ("codex", "claude")
    ]
    runtime = TransportRuntime(
        router=AutoRouter(
            entries=[RunnerEntry(engine=r.engine, runner=r) for r in runners],
            default_engine="codex",
        ),
        projects=ProjectsConfig(
            projects={
                f"projeto_{i}": ProjectConfig(
                    alias=f"projeto_{i}",
                    path=tmp_path,
                    worktrees_dir=Path(".worktrees"),
                )
                for i in range(count)
            },
            default_project="projeto_0",
        ),
        config_path=tmp_path / "takopi.toml",
    )
    cfg = replace(
        make_cfg(transport),
        runtime=runtime,
        chat_id=-100123,
        allowed_user_ids=(42,),
        topics=TelegramTopicsSettings(
            enabled=True, scope="main", session_topics=True, import_native_sessions=True
        ),
    )
    return cfg, transport, runners


def test_help_covers_every_menu_command_and_current_project(tmp_path):
    cfg, _, _ = help_config(tmp_path)
    text = "\n".join(build_help_messages(cfg))
    for entry in build_bot_commands(
        cfg.runtime, include_file=cfg.files.enabled, include_topics=True
    ):
        assert re.search(rf"/{re.escape(entry['command'])}\b", text), entry
    assert "preservando as anteriores" in text
    assert "/new codex projeto_0 revisão" in text
    assert "transferência de arquivos desativada" in text
    assert "CLI ativo" in text


def test_help_scales_with_project_list_and_stays_in_telegram_limits(tmp_path):
    cfg, _, _ = help_config(tmp_path, count=180)
    messages = build_help_messages(cfg)
    assert len(messages) > 1
    assert all(0 < len(text.encode("utf-16-le")) // 2 <= 3500 for text in messages)
    assert "/projeto_179" in "\n".join(messages)
    assert build_bot_commands(cfg.runtime)[0]["command"] == "help"


def test_help_describes_disabled_features_and_legacy_new_behavior(tmp_path):
    cfg, _, _ = help_config(tmp_path)
    cfg = replace(cfg, topics=TelegramTopicsSettings())
    text = "\n".join(build_help_messages(cfg))
    assert "limpa o vínculo" in text
    assert "/resume AGENTE" not in text
    assert "/topic PROJETO" not in text
    assert "/stop" not in text


@pytest.mark.parametrize("mode", ["prompt", "upload"])
@pytest.mark.parametrize("auto_put", [True, False])
def test_help_explains_caption_upload_only_when_enabled(tmp_path, mode, auto_put):
    cfg, _, _ = help_config(tmp_path)
    cfg = replace(
        cfg,
        files=TelegramFilesSettings(
            enabled=True, auto_put=auto_put, auto_put_mode=mode
        ),
    )
    text = "\n".join(build_help_messages(cfg))
    assert ("tarefa na legenda" in text) == (auto_put and mode == "prompt")
    assert ("Sem legenda" in text) == auto_put
    assert "/file put CAMINHO" in text


@pytest.mark.anyio
@pytest.mark.parametrize("command", ["/help", "/help@bot"])
@pytest.mark.parametrize("sender", [42, 999])
async def test_help_routes_without_running_agent_and_keeps_topic_and_authorization(
    tmp_path, command, sender
):
    cfg, transport, runners = help_config(tmp_path)

    async def poller(_cfg):
        yield TelegramIncomingMessage(
            transport="telegram",
            chat_id=cfg.chat_id,
            message_id=4,
            text=command,
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=sender,
            thread_id=88,
            chat_type="supergroup",
            is_forum=True,
        )

    await run_main_loop(cfg, poller)
    assert not any(r.calls for r in runners)
    replies = [
        call
        for call in transport.send_calls
        if call["options"] and call["options"].reply_to
    ]
    assert bool(replies) == (sender == 42)
    if sender == 42:
        assert "Ajuda do bot" in replies[0]["message"].text
        rendered_lines = "\n".join(
            call["message"].text for call in replies
        ).splitlines()
        assert any(line.startswith("/new AGENTE") for line in rendered_lines)
        assert all(call["options"].thread_id == 88 for call in replies)
        assert all(call["options"].reply_to.message_id == 4 for call in replies)
