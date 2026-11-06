from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import secrets
import shlex
import shutil
import tempfile
import time

from anyio.to_thread import run_sync

from ...config import (
    ConfigError,
    ProjectsConfig,
    ensure_table,
    read_config,
    write_config,
)
from ...ids import RESERVED_CHAT_COMMANDS, RESERVED_COMMAND_IDS, is_valid_id
from ...logging import get_logger
from ...settings import validate_settings_data
from ...transport import RenderedMessage, SendOptions
from ...transport_runtime import TransportRuntime
from ..bridge import TelegramBridgeConfig
from ..types import TelegramIncomingMessage
from .reply import make_reply

logger = get_logger(__name__)

CREATE_USAGE = (
    'Use /project_new NOME [CAMINHO]. Exemplo: /project_new meu_app "~/projects/meu app".\n'
    "Sem caminho, cria em ~/projects/NOME. Se a pasta já existe, apenas a cadastra.\n"
    "O nome deve ter de 1 a 32 letras minúsculas, números ou _."
)
DELETE_USAGE = "Use /project_delete PROJETO. Consulte os nomes em /projects."
PROJECT_CALLBACK_PREFIX = "project:delete:"


def _validate_projects(
    runtime: TransportRuntime,
    config: dict,
    config_path: Path,
) -> ProjectsConfig:
    settings = validate_settings_data(config, config_path=config_path)
    return settings.to_projects_config(
        config_path=config_path,
        engine_ids=runtime.engine_ids,
        reserved=RESERVED_CHAT_COMMANDS,
    )


def _create_directory(path: Path, created: list[Path]) -> None:
    if path.is_dir():
        return
    if not path.parent.is_dir():
        _create_directory(path.parent, created)
    path.mkdir()
    created.append(path)


async def handle_project_command(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    command: str,
    args_text: str,
    *,
    reserved: Iterable[str] = (),
    deletions: ProjectDeletions | None = None,
) -> bool:
    """Persist and apply a project mutation; return whether the list changed.

    The read/validate/write/apply sequence has no await: bot commands and the
    config watcher cannot interleave writes on the event loop. write_config
    atomically replaces the file before the live project list is updated.
    """
    reply = make_reply(cfg, msg)
    if not cfg.allowed_user_ids or msg.sender_id not in cfg.allowed_user_ids:
        await reply(
            text="Gerenciar projetos exige um usuário autorizado em allowed_user_ids."
        )
        return False
    usage = CREATE_USAGE if command == "project_new" else DELETE_USAGE
    try:
        args = shlex.split(args_text)
    except ValueError:
        await reply(text=f"Aspas inválidas. {usage}")
        return False
    if not (0 if command == "project_delete" else 1) <= len(args) <= 2:
        await reply(text=usage)
        return False
    alias = args[0].lower() if args else ""
    config_path = cfg.runtime.config_path
    if config_path is None:
        await reply(
            text="O bot está sem um arquivo de configuração para salvar projetos."
        )
        return False
    created: list[Path] = []
    changed = False
    try:
        config = read_config(config_path)
        projects = ensure_table(config, "projects", config_path=config_path)
        existing = next((name for name in projects if name.lower() == alias), None)
        if command == "project_delete" and len(args) <= 1:
            current = _validate_projects(cfg.runtime, config, config_path)
            await (deletions or ProjectDeletions()).prompt(
                cfg, msg, current, existing, alias
            )
            return False
        if command == "project_new":
            if not is_valid_id(alias):
                response = f"Nome de projeto inválido. {CREATE_USAGE}"
            elif alias in RESERVED_COMMAND_IDS | set(reserved) | set(
                cfg.runtime.engine_ids
            ):
                response = (
                    "Esse nome já é usado por um comando ou agente. Escolha outro nome."
                )
            elif existing is not None:
                response = (
                    "Projeto já cadastrado. Consulte /projects ou escolha outro nome."
                )
            else:
                path = (
                    Path(args[1]).expanduser()
                    if len(args) == 2
                    else Path.home() / "projects" / alias
                )
                if not path.is_absolute():
                    await reply(text="Use um caminho absoluto ou começando com ~/.")
                    return False
                path = path.resolve()
                if path.exists() and not path.is_dir():
                    await reply(text="O caminho informado existe e não é uma pasta.")
                    return False
                projects[alias] = {"path": str(path)}
                updated = _validate_projects(cfg.runtime, config, config_path)
                _create_directory(path, created)
                write_config(config, config_path)
                cfg.runtime.update_projects(updated)
                changed = True
                response = (
                    f"Projeto {alias} cadastrado em {path}.\n"
                    f"Use /new {cfg.runtime.default_engine} {alias} para abrir uma sessão."
                )
        elif existing is None:
            response = "Projeto desconhecido. Consulte /projects."
        elif args[1].lower() != "confirmar":
            response = DELETE_USAGE
        else:
            del projects[existing]
            if str(config.get("default_project", "")).lower() == alias:
                config.pop("default_project", None)
            updated = _validate_projects(cfg.runtime, config, config_path)
            write_config(config, config_path)
            cfg.runtime.update_projects(updated)
            changed = True
            response = (
                f"Projeto {existing} removido do Takopi. "
                "Arquivos, tópicos e históricos preservados."
            )
    except (ConfigError, OSError, ValueError, RuntimeError) as exc:
        # Only remove empty directories made by this operation, never user files.
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                logger.warning("project.directory_cleanup.failed", path=str(directory))
        # Validation errors can contain config values, including credentials.
        logger.warning(
            "project.command.failed", command=command, error_type=type(exc).__name__
        )
        response = (
            "Não foi possível salvar o projeto. Verifique a configuração, "
            "o caminho e as permissões de acesso. O cadastro não foi alterado."
        )
    await reply(text=response)
    return changed


@dataclass(frozen=True)
class _Deletion:
    alias: str
    path: Path
    identity: tuple[int, int] | None
    chat_id: int
    thread_id: int | None
    sender_id: int | None
    stage: str
    expires_at: float


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


class ProjectDeletions:
    """Short-lived, single-use confirmations owned by one user and conversation."""

    def __init__(self) -> None:
        self._pending: dict[str, _Deletion] = {}

    def _remember(self, deletion: _Deletion) -> str:
        now = time.monotonic()
        self._pending = {
            key: item for key, item in self._pending.items() if item.expires_at > now
        }
        while len(self._pending) >= 256:
            del self._pending[next(iter(self._pending))]
        token = secrets.token_urlsafe(12)
        self._pending[token] = deletion
        return token

    async def _buttons(
        self,
        cfg: TelegramBridgeConfig,
        msg: TelegramIncomingMessage,
        text: str,
        choices: list[tuple[str, str, str]],
    ) -> None:
        await cfg.exec_cfg.transport.send(
            channel_id=msg.chat_id,
            message=RenderedMessage(
                text=text,
                extra={
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": label,
                                    "callback_data": f"{PROJECT_CALLBACK_PREFIX}{action}:{token}",
                                }
                            ]
                            for label, action, token in choices
                        ]
                    }
                },
            ),
            options=SendOptions(thread_id=msg.thread_id),
        )

    async def prompt(
        self,
        cfg: TelegramBridgeConfig,
        msg: TelegramIncomingMessage,
        current: ProjectsConfig,
        existing: str | None,
        alias: str,
    ) -> None:
        if alias and existing is None:
            await make_reply(cfg, msg)(text="Projeto desconhecido. Consulte /projects.")
            return
        if not current.projects:
            await make_reply(cfg, msg)(
                text="Nenhum projeto cadastrado. Use /project_new NOME."
            )
            return
        choices = []
        for key, project in current.projects.items():
            if alias and key != alias:
                continue
            path = project.path.resolve()
            deletion = _Deletion(
                alias=project.alias,
                path=path,
                identity=_directory_identity(path),
                chat_id=msg.chat_id,
                thread_id=msg.thread_id,
                sender_id=msg.sender_id,
                stage="choice" if alias else "select",
                expires_at=time.monotonic() + 600,
            )
            token = self._remember(deletion)
            if alias:
                await self._choice(cfg, msg, deletion, token)
                return
            choices.append((project.alias, "select", token))
        # Keep each keyboard below Telegram's button limit.
        for start in range(0, len(choices), 50):
            await self._buttons(
                cfg, msg, "Qual projeto deseja excluir?", choices[start : start + 50]
            )

    async def _choice(
        self,
        cfg: TelegramBridgeConfig,
        msg: TelegramIncomingMessage,
        deletion: _Deletion,
        token: str,
    ) -> None:
        await self._buttons(
            cfg,
            msg,
            f"Como deseja excluir o projeto {deletion.alias}?\nPasta: {deletion.path}\n"
            "Excluir apenas do Takopi mantém os arquivos locais. A outra opção apaga a pasta e todo o seu conteúdo.\n"
            "Tópicos e históricos do bot são preservados. Conversas vinculadas deixam de executar tarefas neste projeto.\n"
            f"Alternativa apenas para remover o cadastro: /project_delete {shlex.quote(deletion.alias)} confirmar",
            [
                ("Excluir apenas do Takopi/bot", "keep", token),
                ("Excluir do Takopi/bot e arquivos locais", "files", token),
                ("Cancelar", "cancel", token),
            ],
        )

    async def handle_callback(
        self,
        cfg: TelegramBridgeConfig,
        msg: TelegramIncomingMessage,
        data: str,
        *,
        is_busy: Callable[[], bool] = lambda: False,
    ) -> bool:
        reply = make_reply(cfg, msg)
        if not cfg.allowed_user_ids or msg.sender_id not in cfg.allowed_user_ids:
            await reply(
                text="Gerenciar projetos exige um usuário autorizado em allowed_user_ids."
            )
            return False
        action, _, token = data.removeprefix(PROJECT_CALLBACK_PREFIX).partition(":")
        deletion = self._pending.get(token)
        if (
            deletion is None
            or deletion.expires_at <= time.monotonic()
            or (deletion.chat_id, deletion.thread_id, deletion.sender_id)
            != (msg.chat_id, msg.thread_id, msg.sender_id)
            or action
            not in {
                "select": {"select"},
                "choice": {"keep", "files", "cancel"},
                "confirm": {"delete", "cancel"},
            }[deletion.stage]
        ):
            await reply(
                text="Confirmação inválida ou expirada. Use /project_delete novamente."
            )
            return False
        # Consume before any await, including cancellation and rejected operations.
        del self._pending[token]
        if action == "cancel":
            await reply(text="Exclusão cancelada. Cadastro e arquivos preservados.")
            return False
        config_path = cfg.runtime.config_path
        if config_path is None:
            await reply(
                text="Configuração indisponível. Use /project_delete novamente."
            )
            return False
        try:
            if action == "delete":
                if is_busy():
                    await reply(
                        text="Há tarefas ativas ou pendentes no bot. Aguarde ou cancele antes de apagar arquivos."
                    )
                    return False
                before = _validate_projects(
                    cfg.runtime, read_config(config_path), config_path
                )
                target = before.projects.get(deletion.alias.lower())
                if target is None or target.path.resolve() != deletion.path:
                    await reply(text="O projeto mudou. Use /project_delete novamente.")
                    return False
                error = _unsafe_delete_reason(
                    target.path, before, deletion.alias, config_path
                )
                if error:
                    await reply(
                        text=error + " O cadastro e os arquivos foram preservados."
                    )
                    return False
                if await run_sync(_contains_mount, deletion.path):
                    await reply(
                        text="A pasta contém um ponto de montagem; remova apenas o cadastro."
                    )
                    return False
            # Re-read after asynchronous checks, before changing any local state.
            config = read_config(config_path)
            current = _validate_projects(cfg.runtime, config, config_path)
            project = current.projects.get(deletion.alias.lower())
            if (
                project is None
                or project.path.resolve() != deletion.path
                or _directory_identity(deletion.path) != deletion.identity
            ):
                await reply(
                    text="O projeto ou a pasta mudou. Use /project_delete novamente."
                )
                return False
            if action == "select":
                choice = replace(deletion, stage="choice")
                await self._choice(cfg, msg, choice, self._remember(choice))
                return False
            if action == "files":
                confirmation = replace(deletion, stage="confirm")
                token = self._remember(confirmation)
                await self._buttons(
                    cfg,
                    msg,
                    f"Excluir DEFINITIVAMENTE {deletion.alias} do Takopi e apagar todos os arquivos locais?\n"
                    f"Pasta: {deletion.path}\nEsta ação não pode ser desfeita. Worktrees fora dessa pasta não serão apagados.",
                    [
                        ("Sim, excluir cadastro e apagar arquivos", "delete", token),
                        ("Cancelar", "cancel", token),
                    ],
                )
                return False
            if action == "delete":
                error = _unsafe_delete_reason(
                    project.path, current, deletion.alias, config_path
                )
                if is_busy():
                    error = "Há tarefas ativas ou pendentes no bot. Aguarde ou cancele antes de apagar arquivos."
                if error:
                    await reply(
                        text=error + " O cadastro e os arquivos foram preservados."
                    )
                    return False
            del config["projects"][project.alias]
            if str(config.get("default_project", "")).lower() == deletion.alias.lower():
                config.pop("default_project", None)
            updated = _validate_projects(cfg.runtime, config, config_path)
            if action == "keep":
                write_config(config, config_path)
                cfg.runtime.update_projects(updated)
                await reply(
                    text=f"Projeto {deletion.alias} removido apenas do Takopi. Arquivos, tópicos e históricos preservados."
                )
                return True
            return await _delete_files(cfg, msg, deletion, config, updated, config_path)
        except (ConfigError, OSError, ValueError, RuntimeError) as exc:
            logger.warning("project.delete.failed", error_type=type(exc).__name__)
            await reply(
                text="Não foi possível excluir o projeto. Verifique a configuração e as permissões. Nenhum arquivo foi apagado."
            )
            return False


def _unsafe_delete_reason(
    path: Path,
    projects: ProjectsConfig,
    alias: str,
    config_path: Path,
) -> str | None:
    # Never follow a symlink supplied by the registration, even in an ancestor.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        return "A pasta cadastrada usa um link simbólico; remova apenas o cadastro."
    path = path.resolve()
    protected = (
        Path.home().resolve(),
        Path.cwd().resolve(),
        config_path.resolve(),
        Path(__file__).resolve(),
    )
    if path.parent == Path(path.anchor) or any(
        target.is_relative_to(path) for target in protected
    ):
        return "Essa pasta é protegida pelo sistema, contém o diretório pessoal, o bot ou sua configuração e não pode ser apagada pelo Telegram."
    for key, project in projects.projects.items():
        if key == alias.lower():
            continue
        for other in (project.path.resolve(), project.worktrees_root.resolve()):
            if path.is_relative_to(other) or other.is_relative_to(path):
                return "Essa pasta é compartilhada com outro projeto cadastrado; remova apenas o cadastro."
    if path.is_mount():
        return "A pasta é um ponto de montagem; remova apenas o cadastro."
    if not path.is_dir():
        return "A pasta não está disponível; remova apenas o cadastro."
    return None


def _contains_mount(path: Path) -> bool:
    # Do not traverse a nested filesystem or a link while checking the tree.
    for root, directories, _files in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories if not (Path(root) / name).is_symlink()
        ]
        if any((Path(root) / name).is_mount() for name in directories):
            return True
    return False


async def _delete_files(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    deletion: _Deletion,
    config: dict,
    updated: ProjectsConfig,
    config_path: Path,
) -> bool:
    # Stage on the same filesystem before committing the config. If persistence
    # fails, restore the directory without ever deleting user files.
    reply = make_reply(cfg, msg)
    staging = Path(tempfile.mkdtemp(prefix=".takopi-delete-", dir=deletion.path.parent))
    moved = staging / deletion.path.name
    try:
        deletion.path.rename(moved)
    except OSError:
        staging.rmdir()
        raise
    try:
        write_config(config, config_path)
    except (ConfigError, OSError):
        try:
            moved.rename(deletion.path)
            staging.rmdir()
        except OSError:
            await reply(
                text=f"Falha ao salvar e restaurar a pasta. Nenhum arquivo foi apagado. Recupere os arquivos em {moved}; o cadastro foi mantido."
            )
            return False
        raise
    cfg.runtime.update_projects(updated)
    try:
        await run_sync(shutil.rmtree, staging)
    except OSError:
        await reply(
            text=f"Projeto {deletion.alias} removido do Takopi, mas alguns arquivos não puderam ser apagados. Restante em: {moved}. Verifique as permissões e remova essa pasta manualmente."
        )
    else:
        await reply(
            text=f"Projeto {deletion.alias} removido do Takopi e arquivos locais apagados de {deletion.path}. Tópicos e históricos do bot preservados."
        )
    return True
