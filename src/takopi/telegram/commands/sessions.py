"""Project selection and one Telegram topic per independent conversation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...config import ConfigError
from ...context import RunContext
from ...managed import AiMemoryConfig
from ...model import ResumeToken
from ...native_sessions import (
    NativeSession,
    native_directory_busy,
    native_sessions,
    same_directory,
)
from ...transport import RenderedMessage, SendOptions
from ..files import split_command_args
from ..topic_state import TopicStateStore
from ..topics import _topics_command_error
from ..types import TelegramIncomingMessage
from .reply import make_reply

if TYPE_CHECKING:
    from ..bridge import TelegramBridgeConfig

CALLBACK_PREFIX = "session:new:"


def topic_link(chat_id: int, thread_id: int) -> str | None:
    chat = str(chat_id)
    return f"https://t.me/c/{chat[4:]}/{thread_id}" if chat.startswith("-100") else None


async def _choose(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    text: str,
    choices: list[tuple[str, str]],
) -> None:
    keyboard = []
    for label, arguments in choices:
        data = CALLBACK_PREFIX + arguments
        if len(data.encode()) <= 64:
            keyboard.append([{"text": label, "callback_data": data}])
    await cfg.exec_cfg.transport.send(
        channel_id=msg.chat_id,
        message=RenderedMessage(
            text=text, extra={"reply_markup": {"inline_keyboard": keyboard}}
        ),
        options=SendOptions(thread_id=msg.thread_id),
    )


async def create_session(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    args_text: str,
    store: TopicStateStore,
    imported: NativeSession | None = None,
) -> None:
    reply = make_reply(cfg, msg)
    error = _topics_command_error(cfg, msg.chat_id)
    if error is not None:
        await reply(text=error)
        return
    if msg.chat_type != "supergroup":
        await reply(text="Use /new no grupo com tópicos configurado para este bot.")
        return
    args = split_command_args(args_text)
    engines = tuple(
        e
        for e in cfg.runtime.available_engine_ids()
        if e in {"codex", "claude", "agy", "kiro"}
    )
    if not args:
        await _choose(
            cfg,
            msg,
            "Escolha o agente desta sessão:",
            [("Antigravity (agy)" if e == "agy" else e.title(), e) for e in engines],
        )
        return
    engine = args[0].lower()
    if engine not in engines:
        await reply(
            text="Agente indisponível. Use /new para escolher um agente habilitado."
        )
        return
    aliases = cfg.runtime.project_aliases()
    if len(args) == 1:
        if not aliases:
            await reply(text="Cadastre um projeto antes de criar a sessão.")
            return
        await _choose(
            cfg,
            msg,
            f"Escolha o projeto para {engine}:",
            [(alias, f"{engine} {alias}") for alias in aliases],
        )
        return
    project = cfg.runtime.normalize_project_key(args[1])
    if project is None:
        await reply(text="Projeto desconhecido. Consulte /projects.")
        return
    context = RunContext(project=project)
    try:
        cwd = cfg.runtime.resolve_run_cwd(context)
        if cwd is None or not cwd.is_dir():
            raise ConfigError("O diretório do projeto não está disponível.")
    except ConfigError as exc:
        await reply(text=str(exc))
        return
    name = " ".join(args[2:]) or "nova sessão"
    title = f"{cfg.runtime.project_alias_for_key(project)} · {engine.title()} · {name}"[
        :128
    ]
    creation_key = (
        f"native:{engine}:{imported.session_id}"
        if imported
        else f"{msg.message_id}:{engine}:{project}"
    )
    async with store.creation_lock:
        rows = await store.list_threads(msg.chat_id)
        existing = next((row for row in rows if row.creation_key == creation_key), None)
        if existing is not None:
            thread_id = existing.thread_id
        else:
            created = await cfg.bot.create_forum_topic(msg.chat_id, title)
            if created is None:
                await reply(
                    text="Não consegui criar o tópico. Confira a permissão de gerenciar tópicos."
                )
                return
            thread_id = created.message_thread_id
            await store.create_session_topic(
                msg.chat_id, thread_id, context, engine, title, creation_key
            )
            if imported is not None:
                await store.set_session_resume(
                    msg.chat_id, thread_id, ResumeToken(engine, imported.session_id)
                )
            await cfg.exec_cfg.transport.send(
                channel_id=msg.chat_id,
                message=RenderedMessage(
                    text=(
                        f"Sessão {engine} no projeto {project}.\n"
                        "Envie a primeira tarefa aqui para iniciar a conversa.\n"
                        "Use /model e /reasoning para selecionar modelo e esforço; "
                        "/stop interrompe a tarefa."
                    )
                ),
                options=SendOptions(thread_id=thread_id),
            )
    link = topic_link(msg.chat_id, thread_id)
    await reply(
        text=f"Sessão pronta: [abrir tópico]({link})"
        if link
        else f"Sessão pronta no tópico {thread_id}."
    )


async def list_sessions(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    args_text: str,
    store: TopicStateStore,
) -> None:
    reply = make_reply(cfg, msg)
    args = split_command_args(args_text)
    engines = set(cfg.runtime.available_engine_ids()) & {
        "codex",
        "claude",
        "agy",
        "kiro",
    }
    if len(args) > 2 or (args and args[0] not in engines | {"all"}):
        await reply(text=f"Use /sessions [{'|'.join(sorted(engines))}|all] [página].")
        return
    try:
        page = int(args[1]) if len(args) == 2 else 1
        if page < 1:
            raise ValueError
    except ValueError:
        await reply(text="A página deve ser um número positivo.")
        return
    engine = args[0] if args and args[0] != "all" else None
    rows = [
        r
        for r in await store.list_threads(msg.chat_id)
        if engine is None or engine == r.default_engine or engine in r.sessions
    ]
    entries = []
    for row in rows:
        label = row.topic_title or f"Tópico {row.thread_id}"
        link = topic_link(msg.chat_id, row.thread_id)
        label = label.replace("[", "(").replace("]", ")")
        state = "histórico salvo" if row.sessions else "aguardando primeira tarefa"
        entries.append(
            f"• [{label}]({link}) — {state}" if link else f"• {label} — {state}"
        )
    if cfg.topics.import_native_sessions:
        bound = {(key, value) for row in rows for key, value in row.sessions.items()}
        for native in native_sessions():
            if native.engine not in engines:
                continue
            if (engine and native.engine != engine) or (
                native.engine,
                native.session_id,
            ) in bound:
                continue
            project = _project_for_native(cfg, native)
            status = project or "diretório indisponível/não cadastrado"
            label = (
                " ".join(native.title.split())[:80].replace("[", "(").replace("]", ")")
            )
            entries.append(
                f"• {native.engine} · {status} · {label}\n/resume {native.engine} {native.session_id}"
            )
    lines = [
        f"Sessões ({len(entries)}), página {page}:",
        *entries[(page - 1) * 10 : page * 10],
    ]
    if not entries:
        lines.append("Crie uma sessão com /new.")
    if len(entries) > page * 10:
        lines.append(f"Próxima página: /sessions {engine or 'all'} {page + 1}")
    await reply(text="\n".join(lines))


def _project_for_native(cfg: TelegramBridgeConfig, native: NativeSession) -> str | None:
    if native.cwd is None:
        return None
    for alias in cfg.runtime.project_aliases():
        path = cfg.runtime.resolve_run_cwd(RunContext(project=alias))
        if path is not None and path.is_dir() and same_directory(path, native.cwd):
            return alias
    return None


async def resume_native(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    arguments: str,
    store: TopicStateStore,
) -> None:
    reply = make_reply(cfg, msg)
    if (
        _topics_command_error(cfg, msg.chat_id) is not None
        or msg.chat_type != "supergroup"
    ):
        await reply(text="Use /resume no grupo com tópicos configurado.")
        return
    args = split_command_args(arguments)
    if (
        not cfg.topics.import_native_sessions
        or len(args) != 2
        or args[0]
        not in set(cfg.runtime.available_engine_ids())
        & {"codex", "claude", "agy", "kiro"}
    ):
        await reply(text="Use /resume AGENTE ID, conforme /sessions.")
        return
    for row in await store.list_threads(msg.chat_id):
        if row.sessions.get(args[0]) == args[1]:
            await reply(
                text=f"Sessão já disponível: [abrir tópico]({topic_link(msg.chat_id, row.thread_id)})"
            )
            return
    native = next(
        (
            r
            for r in native_sessions()
            if r.engine == args[0] and r.session_id == args[1]
        ),
        None,
    )
    if native is None:
        await reply(text="Histórico local não encontrado. Consulte /sessions.")
        return
    project = _project_for_native(cfg, native)
    if project is None or native.cwd is None or not native.source.is_file():
        await reply(
            text="O diretório ou arquivo deste histórico não está disponível. Nenhuma sessão foi alterada."
        )
        return
    if native_directory_busy(native.cwd):
        await reply(
            text="Há um CLI ativo neste diretório. Encerre-o no terminal antes de retomar pelo Telegram."
        )
        return
    config_path = cfg.runtime.config_path
    if config_path is None:
        await reply(text="Configuração local indisponível.")
        return
    managed = AiMemoryConfig.from_config({"ai_memory": True}, config_path)
    assert managed is not None
    cwd = cfg.runtime.resolve_run_cwd(RunContext(project=project))
    assert cwd is not None
    managed.allow_import(native.engine, native.session_id, cwd)
    await create_session(
        cfg,
        msg,
        f"{native.engine} {project} histórico {native.session_id[:8]}",
        store,
        native,
    )


async def show_projects(
    cfg: TelegramBridgeConfig, msg: TelegramIncomingMessage
) -> None:
    lines = ["Projetos disponíveis neste WSL. Use /new AGENTE PROJETO:"]
    lines.extend(f"• {alias}" for alias in cfg.runtime.project_aliases())
    lines.extend(
        [
            "\nCriar/cadastrar: /project_new NOME [CAMINHO]",
            "Excluir (escolha manter ou apagar arquivos locais): /project_delete",
        ]
    )
    await make_reply(cfg, msg)(text="\n".join(lines))
