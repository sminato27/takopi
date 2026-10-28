from dataclasses import replace
from pathlib import Path

import pytest

from takopi.config import ProjectConfig, ProjectsConfig
from takopi.model import ResumeToken
from takopi.router import AutoRouter, RunnerEntry
from takopi.runners.mock import Return, ScriptRunner
from takopi.runner_bridge import RunningTask
from takopi.settings import TelegramTopicsSettings
from takopi.telegram.api_models import ForumTopic
from takopi.telegram.commands.sessions import create_session, list_sessions
from takopi.telegram.commands.topics import _handle_new_command
from takopi.telegram.commands.cancel import handle_cancel
from takopi.telegram.loop import run_main_loop
from takopi.telegram.topic_state import TopicStateStore
from takopi.telegram.types import TelegramCallbackQuery, TelegramIncomingMessage
from takopi.transport import MessageRef
from takopi.transport_runtime import TransportRuntime
from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg


class TopicBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.created = []

    async def create_forum_topic(self, chat_id, name):
        self.created.append((chat_id, name))
        return ForumTopic(message_thread_id=100 + len(self.created))


@pytest.fixture
def session_setup(tmp_path):
    transport = FakeTransport()
    bot = TopicBot()
    router = AutoRouter(
        entries=[
            RunnerEntry(engine=e, runner=ScriptRunner([Return(answer="ok")], engine=e))
            for e in ["codex", "claude"]
        ],
        default_engine="codex",
    )
    runtime = TransportRuntime(
        router=router,
        projects=ProjectsConfig(
            projects={
                "test": ProjectConfig(
                    alias="test", path=tmp_path, worktrees_dir=Path(".worktrees")
                )
            },
            default_project="test",
        ),
        config_path=tmp_path / "takopi.toml",
    )
    cfg = replace(
        make_cfg(transport),
        bot=bot,
        runtime=runtime,
        chat_id=-100123,
        topics=TelegramTopicsSettings(enabled=True, scope="main", session_topics=True),
    )
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=-100123,
        message_id=1,
        text="/new",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=1,
        chat_type="supergroup",
        is_forum=True,
    )
    store = TopicStateStore(tmp_path / "topics.json")
    return cfg, msg, store, transport, bot


@pytest.mark.anyio
async def test_new_selector_then_project_then_independent_topics(session_setup):
    cfg, msg, store, transport, bot = session_setup
    await _handle_new_command(cfg, msg, store)
    buttons = transport.send_calls[-1]["message"].extra["reply_markup"][
        "inline_keyboard"
    ]
    assert [r[0]["callback_data"] for r in buttons] == [
        "session:new:codex",
        "session:new:claude",
    ]
    await create_session(cfg, msg, "claude", store)
    assert "projeto" in transport.send_calls[-1]["message"].text
    await create_session(cfg, msg, "claude test revisão", store)
    first = await store.get_thread(msg.chat_id, 101)
    assert first.context.project == "test"
    assert first.default_engine == "claude"
    await store.set_session_resume(msg.chat_id, 101, ResumeToken("claude", "original"))
    # /new inside an existing topic preserves its conversation.
    await _handle_new_command(
        cfg,
        replace(msg, message_id=2, thread_id=101),
        store,
        args_text="codex test correção",
    )
    assert len(bot.created) == 2
    assert (await store.get_thread(msg.chat_id, 101)).sessions == {"claude": "original"}
    assert (await store.get_thread(msg.chat_id, 102)).sessions == {}


@pytest.mark.anyio
async def test_creation_is_idempotent_across_restart_and_listing_is_filtered(
    session_setup,
):
    cfg, msg, store, transport, bot = session_setup
    await create_session(cfg, msg, "claude test", store)
    restarted = TopicStateStore(cfg.runtime.config_path.with_name("topics.json"))
    await create_session(cfg, msg, "claude test", restarted)
    assert len(bot.created) == 1
    await create_session(cfg, replace(msg, message_id=2), "codex test", restarted)
    await list_sessions(cfg, msg, "claude", restarted)
    text = transport.send_calls[-1]["message"].text
    assert "Claude" in text and "Codex" not in text
    assert "t.me/c/123/101" in str(transport.send_calls[-1]["message"].extra)
    await list_sessions(cfg, replace(msg, chat_id=-100999), "", restarted)
    assert "Claude" not in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", ["agy test", "codex missing"])
async def test_invalid_selection_does_not_create_a_topic(session_setup, arguments):
    cfg, msg, store, transport, bot = session_setup
    await create_session(cfg, msg, arguments, store)
    assert not bot.created


@pytest.mark.anyio
async def test_wrong_group_cannot_create_topic(session_setup):
    cfg, msg, store, transport, bot = session_setup
    await create_session(cfg, replace(msg, chat_id=-100999), "codex test", store)
    assert not bot.created


@pytest.mark.anyio
@pytest.mark.parametrize("sender", [1, 999, None])
async def test_create_callback_obeys_owner_allowlist(session_setup, sender):
    cfg, msg, store, transport, bot = session_setup
    cfg = replace(cfg, allowed_user_ids=(1,))

    async def poller(_cfg):
        yield TelegramCallbackQuery(
            transport="telegram",
            chat_id=msg.chat_id,
            message_id=8,
            callback_query_id="create-topic",
            data="session:new:codex test",
            sender_id=sender,
            raw={"message": {"chat": {"type": "supergroup", "is_forum": True}}},
        )

    await run_main_loop(cfg, poller)
    assert len(bot.created) == (1 if sender == 1 else 0)


@pytest.mark.anyio
async def test_stop_without_reply_cancels_only_current_topic(session_setup):
    cfg, msg, store, transport, bot = session_setup
    current = RunningTask()
    other = RunningTask()
    tasks = {
        MessageRef(channel_id=msg.chat_id, message_id=10, thread_id=101): current,
        MessageRef(channel_id=msg.chat_id, message_id=11, thread_id=102): other,
    }
    await handle_cancel(cfg, replace(msg, text="/stop", thread_id=101), tasks)
    assert current.cancel_requested.is_set()
    assert not other.cancel_requested.is_set()


@pytest.mark.anyio
async def test_native_history_lists_and_opens_once_without_launching_agent(
    session_setup, monkeypatch, tmp_path
):
    from takopi.native_sessions import NativeSession
    from takopi.telegram.commands.sessions import resume_native

    cfg, msg, store, transport, bot = session_setup
    cfg = replace(
        cfg, topics=cfg.topics.model_copy(update={"import_native_sessions": True})
    )
    source = tmp_path / "native.jsonl"
    source.write_text("{}")
    native = NativeSession("claude", "native-id", tmp_path, source, "saved history", 1)
    monkeypatch.setattr(
        "takopi.telegram.commands.sessions.native_sessions", lambda: [native]
    )
    monkeypatch.setattr(
        "takopi.telegram.commands.sessions.native_directory_busy", lambda _: False
    )
    monkeypatch.setattr("shutil.which", lambda _: "/bin/echo")
    await list_sessions(cfg, msg, "claude", store)
    assert "/resume claude native-id" in transport.send_calls[-1]["message"].text
    await resume_native(cfg, msg, "claude native-id", store)
    assert (await store.get_thread(msg.chat_id, 101)).sessions == {
        "claude": "native-id"
    }
    await resume_native(cfg, replace(msg, message_id=2), "claude native-id", store)
    assert len(bot.created) == 1
    await list_sessions(cfg, msg, "claude", store)
    assert "Sessões (1)" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
async def test_native_busy_or_missing_project_never_creates_topic(
    session_setup, monkeypatch, tmp_path
):
    from takopi.native_sessions import NativeSession
    from takopi.telegram.commands.sessions import resume_native

    cfg, msg, store, transport, bot = session_setup
    cfg = replace(
        cfg, topics=cfg.topics.model_copy(update={"import_native_sessions": True})
    )
    source = tmp_path / "native.jsonl"
    source.touch()
    native = NativeSession("codex", "native-id", tmp_path, source, "history", 1)
    monkeypatch.setattr(
        "takopi.telegram.commands.sessions.native_sessions", lambda: [native]
    )
    monkeypatch.setattr(
        "takopi.telegram.commands.sessions.native_directory_busy", lambda _: True
    )
    await resume_native(cfg, msg, "codex native-id", store)
    assert not bot.created
    assert "CLI ativo" in transport.send_calls[-1]["message"].text
    monkeypatch.setattr(
        "takopi.telegram.commands.sessions.native_sessions",
        lambda: [replace(native, cwd=tmp_path / "missing")],
    )
    await resume_native(cfg, msg, "codex native-id", store)
    assert not bot.created
    assert "não está disponível" in transport.send_calls[-1]["message"].text
