"""Attachments follow the agent, project and session of their Telegram topic."""

from dataclasses import replace
from pathlib import Path

import pytest

from takopi.config import ProjectConfig, ProjectsConfig
from takopi.context import RunContext
from takopi.model import ResumeToken
from takopi.router import AutoRouter, RunnerEntry
from takopi.runners.mock import Return, ScriptRunner
from takopi.settings import TelegramFilesSettings, TelegramTopicsSettings
from takopi.telegram.api_models import File
from takopi.telegram.loop import run_main_loop
from takopi.telegram.topic_state import TopicStateStore, resolve_state_path
from takopi.telegram.types import TelegramDocument, TelegramIncomingMessage
from takopi.transport_runtime import TransportRuntime
from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg


class UploadBot(FakeBot):
    async def get_file(self, file_id: str) -> File | None:
        return File(file_path=f"files/{file_id}.txt")

    async def download_file(self, file_path: str) -> bytes | None:
        return b"attachment contents"


def upload_message(
    index: int = 1, *, album: bool = False, caption: str = "Leia o anexo"
):
    return TelegramIncomingMessage(
        transport="telegram",
        chat_id=-100123,
        message_id=index,
        thread_id=77,
        text=caption,
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=42,
        chat_type="supergroup",
        is_forum=True,
        media_group_id="album" if album else None,
        document=TelegramDocument(
            file_id=f"doc-{index}",
            file_name=f"note-{index}.txt",
            mime_type="text/plain",
            file_size=19,
            raw={},
        ),
    )


async def upload_config(tmp_path: Path, engine: str):
    project = tmp_path / "topic-project"
    project.mkdir()
    config_path = tmp_path / "takopi.toml"
    runners = {
        name: ScriptRunner([Return(answer="ok")], engine=name)
        for name in ("codex", "claude", "agy")
    }
    runtime = TransportRuntime(
        router=AutoRouter(
            entries=[RunnerEntry(engine=name, runner=r) for name, r in runners.items()],
            default_engine="codex",
        ),
        projects=ProjectsConfig(
            projects={
                name: ProjectConfig(
                    alias=name, path=path, worktrees_dir=Path(".worktrees")
                )
                for name, path in (("topic", project), ("default", tmp_path))
            },
            default_project="default",
        ),
        config_path=config_path,
    )
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        bot=UploadBot(),
        runtime=runtime,
        chat_id=-100123,
        allowed_user_ids=(42,),
        topics=TelegramTopicsSettings(enabled=True, scope="main", session_topics=True),
        files=TelegramFilesSettings(
            enabled=True, auto_put_mode="prompt", allowed_user_ids=[42]
        ),
        media_group_debounce_s=0,
    )
    store = TopicStateStore(resolve_state_path(config_path))
    await store.create_session_topic(
        cfg.chat_id, 77, RunContext(project="topic"), engine, "Attachment test", "test"
    )
    resume = ResumeToken(engine=engine, value=f"existing-{engine}")
    await store.set_session_resume(cfg.chat_id, 77, resume)
    return cfg, transport, runners, project, resume


@pytest.mark.anyio
@pytest.mark.parametrize("engine", ["codex", "claude", "agy"])
@pytest.mark.parametrize("album", [False, True])
async def test_caption_upload_resumes_topic_agent_and_saves_in_its_project(
    tmp_path, engine, album
):
    cfg, transport, runners, project, resume = await upload_config(tmp_path, engine)

    async def poller(_cfg):
        yield upload_message(album=album)
        if album:
            yield upload_message(2, album=True, caption="")

    await run_main_loop(cfg, poller)

    assert len(runners[engine].calls) == 1
    prompt, received_resume = runners[engine].calls[0]
    assert received_resume == resume
    assert prompt.startswith("Leia o anexo")
    for index in range(1, 3 if album else 2):
        path = Path("incoming") / f"note-{index}.txt"
        assert path.as_posix() in prompt
        assert (project / path).read_bytes() == b"attachment contents"
    assert not (tmp_path / "incoming").exists()
    assert all(not runner.calls for name, runner in runners.items() if name != engine)
    replies = [call for call in transport.send_calls if call["options"].reply_to]
    assert replies and all(call["options"].thread_id == 77 for call in replies)


@pytest.mark.anyio
async def test_upload_without_caption_only_saves_file(tmp_path):
    cfg, transport, runners, project, _ = await upload_config(tmp_path, "claude")

    async def poller(_cfg):
        yield upload_message(caption="")

    await run_main_loop(cfg, poller)
    assert (project / "incoming/note-1.txt").read_bytes() == b"attachment contents"
    assert all(not runner.calls for runner in runners.values())
    assert any("saved" in call["message"].text for call in transport.send_calls)


@pytest.mark.anyio
@pytest.mark.parametrize("album", [False, True])
async def test_disabled_upload_explains_why_instead_of_ignoring_attachment(
    tmp_path, album
):
    cfg, transport, runners, project, _ = await upload_config(tmp_path, "codex")
    cfg = replace(cfg, files=TelegramFilesSettings(enabled=False))

    async def poller(_cfg):
        yield upload_message(album=album)

    await run_main_loop(cfg, poller)
    assert not (project / "incoming").exists()
    assert all(not runner.calls for runner in runners.values())
    assert any("desativado" in call["message"].text for call in transport.send_calls)
