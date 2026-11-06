from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from takopi.config import ConfigError, read_config, write_config
from takopi.context import RunContext
from takopi.telegram.commands import projects
from takopi.telegram.commands.projects import ProjectDeletions, handle_project_command
from takopi.telegram.loop import run_main_loop
from takopi.telegram.topic_state import TopicStateStore, resolve_state_path
from takopi.telegram.types import TelegramCallbackQuery
from tests import test_telegram_projects as project_tests

project_setup = project_tests.project_setup


def button(transport, action):
    rows = transport.send_calls[-1]["message"].extra["reply_markup"]["inline_keyboard"]
    return next(
        item["callback_data"]
        for row in rows
        for item in row
        if item["callback_data"].startswith(f"project:delete:{action}:")
    )


async def prompt(setup, manager, args="original"):
    cfg, msg, transport, _ = setup
    assert not await handle_project_command(
        cfg, msg, "project_delete", args, deletions=manager
    )
    return transport.send_calls[-1]["message"]


async def confirm_files(setup, manager):
    cfg, msg, transport, _ = setup
    await prompt(setup, manager)
    assert not await manager.handle_callback(cfg, msg, button(transport, "files"))
    return button(transport, "delete")


@pytest.mark.anyio
async def test_selector_shows_modes_and_remove_only_preserves_files(project_setup):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    before = config_path.read_bytes()
    message = await prompt(project_setup, manager, "")
    assert "Qual projeto" in message.text
    assert not await manager.handle_callback(cfg, msg, button(transport, "select"))
    message = transport.send_calls[-1]["message"]
    assert str(config_path.parent / "original") in message.text
    assert "Excluir apenas do Takopi/bot" in str(message.extra)
    assert "Excluir do Takopi/bot e arquivos locais" in str(message.extra)
    assert config_path.read_bytes() == before
    assert await manager.handle_callback(cfg, msg, button(transport, "keep"))
    assert (config_path.parent / "original/keep.txt").read_text() == "keep my files"
    assert read_config(config_path)["projects"] == {}
    assert "default_project" not in read_config(config_path)
    assert cfg.runtime.project_aliases() == ()
    assert transport.send_calls[-1]["options"].thread_id == msg.thread_id


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["choice", "confirm"])
async def test_cancel_and_replayed_buttons_cannot_delete(project_setup, stage):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    before = config_path.read_bytes()
    await prompt(project_setup, manager)
    if stage == "confirm":
        assert not await manager.handle_callback(cfg, msg, button(transport, "files"))
    destructive = button(transport, "delete" if stage == "confirm" else "keep")
    assert not await manager.handle_callback(cfg, msg, button(transport, "cancel"))
    assert "cancelada" in transport.send_calls[-1]["message"].text
    assert not await manager.handle_callback(cfg, msg, destructive)
    assert "expirada" in transport.send_calls[-1]["message"].text
    assert config_path.read_bytes() == before
    assert (config_path.parent / "original/keep.txt").exists()


@pytest.mark.anyio
async def test_disk_delete_requires_second_confirmation_and_preserves_link_targets(
    project_setup,
):
    cfg, msg, transport, config_path = project_setup
    root = config_path.parent / "original"
    outside = config_path.parent / "outside"
    outside.mkdir()
    (outside / "important").write_text("keep")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / ".hidden").write_text("delete")
    (root / "nested").mkdir()
    (root / "nested" / "file").write_text("delete")
    history = config_path.with_name("telegram_topic_state.json")
    history.write_text("preserve history")
    manager = ProjectDeletions()
    before = config_path.read_bytes()
    confirm = await confirm_files(project_setup, manager)
    assert config_path.read_bytes() == before
    assert (root / "keep.txt").exists()
    assert str(root) in transport.send_calls[-1]["message"].text
    assert "não pode ser desfeita" in transport.send_calls[-1]["message"].text
    assert await manager.handle_callback(cfg, msg, confirm)
    assert not root.exists()
    assert (outside / "important").read_text() == "keep"
    assert history.read_text() == "preserve history"
    assert read_config(config_path)["projects"] == {}
    assert read_config(config_path)["codex"]["model"] == "keep-model"
    assert cfg.runtime.project_aliases() == ()
    assert not list(config_path.parent.glob(".takopi-delete-*"))
    # A new registration is never affected by replaying an old confirmation.
    assert await handle_project_command(cfg, msg, "project_new", f"original {root}")
    assert not await manager.handle_callback(cfg, msg, confirm)
    assert root.is_dir()
    assert cfg.runtime.project_aliases() == ("original",)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["user", "unknown_user", "anonymous", "chat", "topic", "allowlist"]
)
async def test_confirmations_are_bound_to_authorized_user_and_conversation(
    project_setup,
    change,
):
    cfg, msg, transport, config_path = project_setup
    cfg = replace(cfg, allowed_user_ids=(42, 43))
    setup = cfg, msg, transport, config_path
    manager = ProjectDeletions()
    data = await confirm_files(setup, manager)
    before = config_path.read_bytes()
    other = {
        "user": replace(msg, sender_id=43),
        "unknown_user": replace(msg, sender_id=999),
        "anonymous": replace(msg, sender_id=None),
        "chat": replace(msg, chat_id=-100999),
        "topic": replace(msg, thread_id=99),
        "allowlist": msg,
    }[change]
    denied_cfg = replace(cfg, allowed_user_ids=()) if change == "allowlist" else cfg
    assert not await manager.handle_callback(denied_cfg, other, data)
    assert config_path.read_bytes() == before
    assert (config_path.parent / "original/keep.txt").exists()
    # An unauthorized click does not consume the owner's confirmation.
    assert await manager.handle_callback(cfg, msg, data)


@pytest.mark.anyio
async def test_forged_stage_expiration_and_restart_preserve_files(
    project_setup, monkeypatch
):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    await prompt(project_setup, manager)
    data = button(transport, "files")
    assert not await manager.handle_callback(
        cfg, msg, data.replace(":files:", ":delete:")
    )
    assert not await ProjectDeletions().handle_callback(cfg, msg, data)
    now = projects.time.monotonic()
    monkeypatch.setattr(projects, "time", SimpleNamespace(monotonic=lambda: now + 601))
    assert not await manager.handle_callback(cfg, msg, data)
    assert "expirada" in transport.send_calls[-1]["message"].text
    assert (config_path.parent / "original/keep.txt").exists()
    assert "Original" in read_config(config_path)["projects"]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["path", "removed", "directory", "symlink"])
async def test_changed_targets_require_new_confirmation(project_setup, change):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    data = await confirm_files(project_setup, manager)
    config = read_config(config_path)
    root = config_path.parent / "original"
    other = config_path.parent / "other"
    if change == "path":
        other.mkdir()
        config["projects"]["Original"]["path"] = str(other)
    elif change == "removed":
        config["projects"] = {}
        config.pop("default_project")
    else:
        root.rename(other)
        if change == "symlink":
            root.symlink_to(other, target_is_directory=True)
        else:
            root.mkdir()
            (root / "new").write_text("new project")
    write_config(config, config_path)
    before = config_path.read_bytes()
    assert not await manager.handle_callback(cfg, msg, data)
    assert config_path.read_bytes() == before
    assert (
        (other / "keep.txt").exists()
        if change in {"directory", "symlink"}
        else root.exists()
    )


@pytest.mark.anyio
@pytest.mark.parametrize("overlap", ["same", "child", "parent", "worktree"])
async def test_shared_directory_is_never_deleted(project_setup, overlap):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    data = await confirm_files(project_setup, manager)
    root = config_path.parent / "original"
    config = read_config(config_path)
    path = {
        "same": root,
        "child": root / "nested",
        "parent": root.parent,
        "worktree": root.parent / "other",
    }[overlap]
    config["projects"]["other"] = {"path": str(path)}
    if overlap == "worktree":
        config["projects"]["other"]["worktrees_dir"] = str(root / "worktrees")
    write_config(config, config_path)
    before = config_path.read_bytes()
    assert not await manager.handle_callback(cfg, msg, data)
    assert "compartilhada" in transport.send_calls[-1]["message"].text
    assert config_path.read_bytes() == before
    assert (root / "keep.txt").exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "target",
    [
        "home",
        "root",
        "cwd",
        "config",
        "link",
        "missing",
        "file",
        "mount",
        "nested_mount",
    ],
)
async def test_unsafe_directories_are_refused(project_setup, monkeypatch, target):
    cfg, msg, transport, config_path = project_setup
    root = config_path.parent / "original"
    path = root
    if target == "home":
        path = Path.home()
    elif target == "root":
        path = Path("/")
    elif target == "cwd":
        path = Path.cwd()
    elif target == "config":
        monkeypatch.setattr(
            Path, "home", classmethod(lambda cls: Path("/nonexistent-home"))
        )
        path = config_path.parent
    elif target == "link":
        path = root.parent / "link"
        path.symlink_to(root, target_is_directory=True)
    elif target == "missing":
        path = root.parent / "missing"
    elif target == "file":
        path = root / "keep.txt"
    elif target == "mount":
        monkeypatch.setattr(Path, "is_mount", lambda self: self == root)
    elif target == "nested_mount":
        (root / "mount").mkdir()
        monkeypatch.setattr(Path, "is_mount", lambda self: self == root / "mount")
    config = read_config(config_path)
    config["projects"]["Original"]["path"] = str(path)
    write_config(config, config_path)
    manager = ProjectDeletions()
    data = await confirm_files(project_setup, manager)
    before = config_path.read_bytes()
    assert not await manager.handle_callback(cfg, msg, data)
    assert config_path.read_bytes() == before
    assert (root / "keep.txt").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("busy_after_scan", [False, True])
async def test_busy_bot_keeps_registration_and_files(
    project_setup, monkeypatch, busy_after_scan
):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    data = await confirm_files(project_setup, manager)
    busy = not busy_after_scan

    def scan(_path):
        nonlocal busy
        busy = True
        return False

    monkeypatch.setattr(projects, "_contains_mount", scan)
    assert not await manager.handle_callback(cfg, msg, data, is_busy=lambda: busy)
    assert "tarefas ativas" in transport.send_calls[-1]["message"].text
    assert "Original" in read_config(config_path)["projects"]
    assert (config_path.parent / "original/keep.txt").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["save", "rename", "cleanup", "restore"])
async def test_failure_recovery_preserves_or_reports_remaining_files(
    project_setup,
    monkeypatch,
    failure,
):
    cfg, msg, transport, config_path = project_setup
    manager = ProjectDeletions()
    data = await confirm_files(project_setup, manager)
    root = config_path.parent / "original"
    before = config_path.read_bytes()

    def fail(*_args, **_kwargs):
        raise PermissionError("secret must not be disclosed")

    def fail_write(*_args):
        raise ConfigError("secret must not be disclosed")

    if failure in {"save", "restore"}:
        monkeypatch.setattr(projects, "write_config", fail_write)
    if failure == "rename":
        monkeypatch.setattr(Path, "rename", fail)
    if failure == "cleanup":
        monkeypatch.setattr(projects.shutil, "rmtree", fail)
    if failure == "restore":
        rename = Path.rename

        def fail_restore(self, target):
            if target == root:
                raise PermissionError("cannot restore")
            return rename(self, target)

        monkeypatch.setattr(Path, "rename", fail_restore)
    changed = await manager.handle_callback(cfg, msg, data)
    assert changed == (failure == "cleanup")
    assert "secret" not in transport.send_calls[-1]["message"].text
    if failure in {"save", "rename"}:
        assert (root / "keep.txt").read_text() == "keep my files"
        assert not list(root.parent.glob(".takopi-delete-*"))
    else:
        remaining = next(root.parent.glob(".takopi-delete-*/original"))
        assert (remaining / "keep.txt").read_text() == "keep my files"
        assert str(remaining) in transport.send_calls[-1]["message"].text
    if failure != "cleanup":
        assert config_path.read_bytes() == before
        assert cfg.runtime.project_aliases() == ("Original",)
    else:
        assert cfg.runtime.project_aliases() == ()
        assert read_config(config_path)["projects"] == {}


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["keep", "files"])
async def test_callbacks_route_in_topic_and_refresh_menu_without_agent(
    project_setup, mode
):
    cfg, msg, transport, config_path = project_setup
    runner = cfg.runtime.resolve_runner(resume_token=None, engine_override=None).runner

    def callback(action, number):
        data = button(transport, action)
        assert len(data.encode()) <= 64
        return TelegramCallbackQuery(
            transport="telegram",
            chat_id=msg.chat_id,
            message_id=1,
            callback_query_id=f"callback-{number}",
            sender_id=msg.sender_id,
            data=data,
            raw={
                "message": {
                    "message_thread_id": msg.thread_id,
                    "chat": {"type": "supergroup"},
                }
            },
        )

    async def poller(_cfg):
        yield replace(msg, text="/project_delete")
        yield callback("select", 1)
        yield callback(mode, 2)
        if mode == "files":
            yield callback("delete", 3)

    await run_main_loop(cfg, poller)
    assert not runner.calls
    assert len(cfg.bot.callback_calls) == (3 if mode == "files" else 2)
    assert read_config(config_path)["projects"] == {}
    assert (config_path.parent / "original").exists() == (mode == "keep")
    menu = {item["command"] for item in cfg.bot.command_calls[-1]["commands"]}
    assert "original" not in menu
    assert {"projects", "project_new", "project_delete"} <= menu
    assert all(
        call["options"].thread_id == msg.thread_id for call in transport.send_calls
    )


@pytest.mark.anyio
async def test_run_starting_before_progress_message_blocks_disk_delete(
    project_setup, monkeypatch
):
    cfg, msg, transport, config_path = project_setup
    store = TopicStateStore(resolve_state_path(config_path))
    await store.create_session_topic(
        msg.chat_id,
        msg.thread_id,
        RunContext(project="original"),
        "codex",
        "work",
        "test",
    )
    started, finished = anyio.Event(), anyio.Event()

    async def starting_run(**_kwargs):
        started.set()
        await finished.wait()

    monkeypatch.setattr("takopi.telegram.loop.run_engine", starting_run)

    def callback(data, number):
        return TelegramCallbackQuery(
            transport="telegram",
            chat_id=msg.chat_id,
            message_id=1,
            callback_query_id=str(number),
            sender_id=msg.sender_id,
            data=data,
            raw={
                "message": {
                    "message_thread_id": msg.thread_id,
                    "chat": {"type": "supergroup"},
                }
            },
        )

    async def poller(_cfg):
        yield replace(msg, text="/project_delete original")
        yield callback(button(transport, "files"), 1)
        confirmation = button(transport, "delete")
        yield replace(msg, message_id=11, text="work in project")
        with anyio.fail_after(2):
            await started.wait()
        try:
            yield callback(confirmation, 2)
        finally:
            finished.set()

    await run_main_loop(cfg, poller)
    assert "tarefas ativas" in transport.send_calls[-1]["message"].text
    assert (config_path.parent / "original/keep.txt").exists()
    assert "Original" in read_config(config_path)["projects"]
