from dataclasses import replace
from pathlib import Path
import shlex

import anyio
import pytest

from takopi.config import (
    ConfigError,
    ProjectConfig,
    ProjectsConfig,
    read_config,
    write_config,
)
from takopi.context import RunContext
from takopi.model import ResumeToken
from takopi.settings import TelegramTopicsSettings, load_settings
from takopi.telegram.commands.menu import build_bot_commands
from takopi.telegram.commands.projects import handle_project_command
from takopi.telegram.commands.sessions import create_session, show_projects
from takopi.telegram.loop import run_main_loop
from takopi.telegram.topic_state import TopicStateStore, resolve_state_path
from takopi.telegram.types import TelegramIncomingMessage
from tests.telegram_fakes import FakeTransport, make_cfg


@pytest.fixture
def project_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    transport = FakeTransport()
    cfg = replace(
        make_cfg(transport),
        chat_id=-100123,
        allowed_user_ids=(42,),
        topics=TelegramTopicsSettings(enabled=True, scope="main", session_topics=True),
        forward_coalesce_s=0,
    )
    path = tmp_path / "takopi.toml"
    project = tmp_path / "original"
    project.mkdir()
    (project / "keep.txt").write_text("keep my files")
    write_config(
        {
            "default_engine": "codex",
            "default_project": "Original",
            "projects": {"Original": {"path": str(project)}},
            "transports": {
                "telegram": {"bot_token": "secret-test-token", "chat_id": cfg.chat_id}
            },
            "codex": {"model": "keep-model"},
        },
        path,
    )
    # Keep the fake runner while applying the real settings contract.
    cfg.runtime._config_path = path
    settings, _ = load_settings(path)
    cfg.runtime.update_projects(
        settings.to_projects_config(config_path=path, engine_ids=("codex",))
    )
    msg = TelegramIncomingMessage(
        transport="telegram",
        chat_id=cfg.chat_id,
        message_id=10,
        text="",
        reply_to_message_id=None,
        reply_to_text=None,
        sender_id=42,
        thread_id=88,
        chat_type="supergroup",
        is_forum=True,
    )
    return cfg, msg, transport, path


@pytest.mark.anyio
@pytest.mark.parametrize("explicit_path", [False, True])
async def test_create_persists_and_is_immediately_available_for_sessions(
    project_setup, explicit_path
):
    cfg, msg, transport, config_path = project_setup
    path = config_path.parent / "nested" / "with spaces"
    args = f"My_App {shlex.quote(str(path))}" if explicit_path else "My_App"
    expected = path if explicit_path else config_path.parent / "projects" / "my_app"
    assert await handle_project_command(cfg, msg, "project_new", args)
    assert expected.is_dir()
    assert cfg.runtime.resolve_run_cwd(RunContext(project="my_app")) == expected
    settings, _ = load_settings(config_path)
    assert settings.projects["my_app"].path == str(expected)
    assert settings.default_project == "Original"
    assert read_config(config_path)["codex"] == {"model": "keep-model"}
    await show_projects(cfg, msg)
    assert "my_app" in transport.send_calls[-1]["message"].text
    store = TopicStateStore(resolve_state_path(config_path))
    await create_session(cfg, msg, "codex", store)
    assert "session:new:codex my_app" in str(transport.send_calls[-1]["message"].extra)
    await create_session(cfg, msg, "codex my_app", store)
    thread = await store.get_thread(msg.chat_id, 1)
    assert thread is not None and thread.context is not None
    assert thread.context.project == "my_app"


@pytest.mark.anyio
async def test_register_existing_path_preserves_files_and_duplicate_alias_is_rejected(
    project_setup,
):
    cfg, msg, transport, config_path = project_setup
    assert await handle_project_command(cfg, msg, "project_new", 'copy "~/original"')
    original_config = config_path.read_bytes()
    assert not await handle_project_command(
        cfg, msg, "project_new", 'COPY "~/different"'
    )
    assert config_path.read_bytes() == original_config
    assert (config_path.parent / "original/keep.txt").read_text() == "keep my files"
    assert not (config_path.parent / "different").exists()
    assert "já cadastrado" in transport.send_calls[-1]["message"].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args",
    [
        "",
        "too many args",
        'bad "quote',
        "../escape",
        "a-b",
        "a" * 33,
        "codex",
        "help",
        "project_new",
        "project_delete",
        "plugin_cmd",
        "name relative/path",
    ],
)
async def test_invalid_create_never_changes_config_or_directories(project_setup, args):
    cfg, msg, transport, config_path = project_setup
    before = config_path.read_bytes()
    assert not await handle_project_command(
        cfg, msg, "project_new", args, reserved={"plugin_cmd"}
    )
    assert config_path.read_bytes() == before
    assert not (config_path.parent / "projects").exists()
    assert transport.send_calls[-1]["options"].thread_id == msg.thread_id


@pytest.mark.anyio
async def test_file_path_is_rejected(project_setup):
    cfg, msg, transport, config_path = project_setup
    before = config_path.read_bytes()
    assert not await handle_project_command(
        cfg, msg, "project_new", f"bad {shlex.quote(str(config_path))}"
    )
    assert "não é uma pasta" in transport.send_calls[-1]["message"].text
    assert config_path.read_bytes() == before


@pytest.mark.anyio
async def test_delete_requires_confirmation_and_preserves_bound_session_and_files(
    project_setup,
):
    cfg, msg, transport, config_path = project_setup
    before = config_path.read_bytes()
    store = TopicStateStore(resolve_state_path(config_path))
    await store.create_session_topic(
        msg.chat_id, 88, RunContext(project="original"), "codex", "history", "original"
    )
    await store.set_session_resume(
        msg.chat_id, 88, ResumeToken("codex", "keep-session")
    )
    state_before = resolve_state_path(config_path).read_bytes()
    assert not await handle_project_command(cfg, msg, "project_delete", "ORIGINAL")
    assert (
        str(config_path.parent / "original") in transport.send_calls[-1]["message"].text
    )
    assert (
        "/project_delete Original confirmar" in transport.send_calls[-1]["message"].text
    )
    assert config_path.read_bytes() == before
    assert await handle_project_command(
        cfg, msg, "project_delete", "original confirmar"
    )
    settings, _ = load_settings(config_path)
    assert settings.projects == {}
    assert settings.default_project is None
    assert cfg.runtime.project_aliases() == ()
    assert cfg.runtime.default_project is None
    assert (config_path.parent / "original/keep.txt").read_text() == "keep my files"
    assert resolve_state_path(config_path).read_bytes() == state_before
    with pytest.raises(ConfigError, match="unknown project"):
        cfg.runtime.resolve_run_cwd(RunContext(project="original"))

    # A new run in the old topic must fail instead of using the bot's cwd.
    async def poller(_cfg):
        yield replace(msg, message_id=11, text="continue")

    runner = cfg.runtime.resolve_runner(resume_token=None, engine_override=None).runner
    await run_main_loop(cfg, poller)
    assert not runner.calls
    assert any(
        "unknown project" in call["message"].text for call in transport.send_calls
    )
    preserved = await TopicStateStore(resolve_state_path(config_path)).get_thread(
        msg.chat_id, 88
    )
    assert preserved is not None
    assert preserved.sessions == {"codex": "keep-session"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args", ["", "original nope", "original confirmar extra", "unknown confirmar"]
)
async def test_invalid_delete_keeps_registration(project_setup, args):
    cfg, msg, _, config_path = project_setup
    before = config_path.read_bytes()
    assert not await handle_project_command(cfg, msg, "project_delete", args)
    assert config_path.read_bytes() == before
    assert cfg.runtime.normalize_project_key("original") == "original"


@pytest.mark.anyio
@pytest.mark.parametrize("existing_dir", [False, True])
@pytest.mark.parametrize("command", ["project_new", "project_delete"])
async def test_write_failure_preserves_config_runtime_and_existing_files(
    project_setup, monkeypatch, existing_dir, command
):
    cfg, msg, transport, config_path = project_setup
    before = config_path.read_bytes()
    project_path = config_path.parent / "new-parent" / "new-child"
    if existing_dir:
        project_path.mkdir(parents=True)
        (project_path / "keep").write_text("mine")

    def fail_write(*_args):
        raise ConfigError("sensitive config values must not be shown")

    monkeypatch.setattr("takopi.telegram.commands.projects.write_config", fail_write)
    args = (
        f"added {shlex.quote(str(project_path))}"
        if command == "project_new"
        else "original confirmar"
    )
    assert not await handle_project_command(cfg, msg, command, args)
    assert "Não foi possível" in transport.send_calls[-1]["message"].text
    assert "sensitive" not in transport.send_calls[-1]["message"].text
    assert config_path.read_bytes() == before
    assert cfg.runtime.project_aliases() == ("Original",)
    assert cfg.runtime.default_project == "original"
    if existing_dir:
        assert (project_path / "keep").read_text() == "mine"
    else:
        assert not project_path.parent.exists()


@pytest.mark.anyio
async def test_directory_permission_failure_leaves_no_registration(
    project_setup, monkeypatch
):
    cfg, msg, _, config_path = project_setup
    before = config_path.read_bytes()

    def fail_mkdir(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    assert not await handle_project_command(cfg, msg, "project_new", "denied")
    assert config_path.read_bytes() == before
    assert cfg.runtime.project_aliases() == ("Original",)


@pytest.mark.anyio
@pytest.mark.parametrize("sender", [42, 999, None])
@pytest.mark.parametrize(
    "command,args", [("project_new", "added"), ("project_delete", "original confirmar")]
)
async def test_commands_route_with_authorization_and_update_menu_without_agent(
    project_setup, sender, command, args
):
    cfg, msg, transport, config_path = project_setup
    before = config_path.read_bytes()
    runner = cfg.runtime.resolve_runner(resume_token=None, engine_override=None).runner

    async def poller(_cfg):
        yield replace(msg, text=f"/{command}@bot {args}", sender_id=sender)

    await run_main_loop(cfg, poller)
    assert not runner.calls
    assert (config_path.read_bytes() != before) == (sender == 42)
    if sender == 42:
        menu = {c["command"] for c in cfg.bot.command_calls[-1]["commands"]}
        assert {"project_new", "project_delete", "projects"} <= menu
        assert ("added" in menu) == (command == "project_new")
        assert ("original" in menu) == (command != "project_delete")
        assert transport.send_calls[-1]["options"].thread_id == msg.thread_id


@pytest.mark.anyio
async def test_no_allowlist_disables_project_management(project_setup):
    cfg, msg, transport, config_path = project_setup
    before = config_path.read_bytes()
    cfg = replace(cfg, allowed_user_ids=())
    assert not await handle_project_command(cfg, msg, "project_new", "added")
    assert "allowed_user_ids" in transport.send_calls[-1]["message"].text
    assert config_path.read_bytes() == before


@pytest.mark.anyio
async def test_concurrent_creates_preserve_both_registrations(project_setup):
    cfg, msg, _, config_path = project_setup
    async with anyio.create_task_group() as tg:
        tg.start_soon(handle_project_command, cfg, msg, "project_new", "first")
        tg.start_soon(handle_project_command, cfg, msg, "project_new", "second")
    assert set(read_config(config_path)["projects"]) == {"Original", "first", "second"}
    assert set(cfg.runtime.project_aliases()) == {"Original", "first", "second"}


def test_management_commands_survive_menu_limit(project_setup):
    cfg, _, _, _ = project_setup
    cfg.runtime.update_projects(
        ProjectsConfig(
            projects={
                f"project_{i}": ProjectConfig(
                    alias=f"project_{i}",
                    path=Path("/tmp"),
                    worktrees_dir=Path(".worktrees"),
                )
                for i in range(180)
            }
        )
    )
    menu = build_bot_commands(cfg.runtime, include_topics=False)
    assert len(menu) == 100
    assert {"projects", "project_new", "project_delete"} <= {
        entry["command"] for entry in menu
    }


@pytest.mark.anyio
@pytest.mark.parametrize("config_error", ["missing", "malformed", "invalid"])
async def test_unusable_config_never_changes_runtime_or_creates_directory(
    project_setup, config_error
):
    cfg, msg, transport, config_path = project_setup
    if config_error == "missing":
        config_path.unlink()
    elif config_error == "malformed":
        config_path.write_text("broken = [")
    else:
        config = read_config(config_path)
        config["transports"]["telegram"]["chat_id"] = "secret-test-token"
        write_config(config, config_path)
    assert not await handle_project_command(cfg, msg, "project_new", "added")
    assert "secret-test-token" not in transport.send_calls[-1]["message"].text
    assert cfg.runtime.project_aliases() == ("Original",)
    assert not (config_path.parent / "projects").exists()
