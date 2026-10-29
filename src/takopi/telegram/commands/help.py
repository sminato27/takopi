"""Portuguese command guide reflecting the bot's current configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...ids import RESERVED_CHAT_COMMANDS
from ..types import TelegramIncomingMessage
from .menu import build_bot_commands
from .reply import make_reply

if TYPE_CHECKING:
    from ..bridge import TelegramBridgeConfig


def build_help_messages(cfg: TelegramBridgeConfig) -> list[str]:
    aliases = list(cfg.runtime.project_aliases())
    engines = list(cfg.runtime.available_engine_ids())
    project = "teste" if "teste" in aliases else (aliases[0] if aliases else "PROJETO")
    engine = cfg.runtime.default_engine
    lines = [
        "**Ajuda do bot**",
        "/help — mostra este guia. Palavras em MAIÚSCULAS são valores para substituir; [colchetes] indicam algo opcional.",
        "",
        "**Conversas e projetos**",
    ]
    if cfg.topics.enabled and cfg.topics.session_topics:
        lines.extend(
            [
                "/new — escolhe agente e projeto por botões.",
                "/new AGENTE — escolhe o projeto e cria outro tópico.",
                "/new AGENTE PROJETO [nome da conversa] — cria uma sessão independente, preservando as anteriores.",
                f"Exemplo: `/new {engine} {project} revisão`. Depois, escreva sua tarefa dentro do tópico criado.",
            ]
        )
    else:
        lines.append(
            "/new — limpa o vínculo com a sessão atual para começar outra conversa; mantém o projeto/contexto."
        )
    lines.append("/projects — lista os projetos cadastrados e exemplos de uso.")
    if "agy" in engines:
        lines.append(
            "Antigravity usa o nome `agy`: `/new agy` cria uma conversa e `/sessions agy` lista seus históricos."
        )
    if cfg.topics.enabled:
        lines.extend(
            [
                f"/sessions [{'|'.join(engines)}|all] [página] — lista sessões; exemplo: `/sessions {engine}` ou `/sessions all 2`.",
            ]
        )
        if cfg.topics.import_native_sessions:
            lines.extend(
                [
                    "/resume AGENTE ID — abre um histórico local em um tópico, ou retorna o tópico já existente. Copie o comando exibido em /sessions.",
                    "A retomada é recusada se o diretório não existir ou houver um CLI ativo nele. Encerre o CLI no terminal antes de alternar para o Telegram.",
                ]
            )
        else:
            lines.append(
                "/resume — importação de históricos locais desativada nesta instalação."
            )
        lines.append("/stop — interrompe a tarefa ativa deste tópico.")
    lines.extend(
        [
            "/cancel — responda à mensagem de progresso para cancelar aquela tarefa, inclusive uma tarefa na fila.",
            "",
            "**Agente, modelo e respostas**",
            "/agent — mostra o agente padrão; `/agent set AGENTE` escolhe outro; `/agent clear` remove a escolha local.",
            "/model — mostra o modelo; `/model set [AGENTE] MODELO` define; `/model clear [AGENTE]` remove a escolha local.",
            "/reasoning — mostra o esforço e os níveis disponíveis; `/reasoning set [AGENTE] NÍVEL` define; `/reasoning clear [AGENTE]` remove a escolha local.",
            "/trigger — mostra quando o bot responde; `/trigger all` aceita mensagens normais; `/trigger mentions` exige menção, resposta ao bot ou comando; `/trigger clear` restaura o padrão.",
            "Essas escolhas valem para o tópico atual; fora de um tópico, valem para o chat. Remover uma escolha local volta a usar o padrão herdado.",
            "",
            "**Contexto e branches**",
            "/ctx — mostra o projeto e a branch; `/ctx set PROJETO [@branch]` altera o vínculo; `/ctx clear` remove o vínculo.",
            "Para trocar o projeto de uma conversa já iniciada, crie outra sessão com /new.",
        ]
    )
    if cfg.topics.enabled:
        lines.append(
            "/topic PROJETO @branch — cria ou reutiliza um tópico associado à branch. A tarefa roda em uma área Git separada (worktree)."
        )
    if cfg.files.enabled:
        lines.extend(
            [
                "/file put CAMINHO — envie um documento com esse comando na legenda, ou responda ao documento, para salvá-lo no projeto.",
                "/file get CAMINHO — envia um arquivo ou diretório do projeto ao Telegram.",
            ]
        )
    else:
        lines.append("/file — transferência de arquivos desativada nesta instalação.")
    lines.extend(["", "**Atalhos por mensagem**"])
    lines.extend(f"/{name} TAREFA — usa {name} nessa mensagem." for name in engines)
    if aliases:
        lines.append(
            f"Os atalhos de projeto abaixo selecionam onde trabalhar. Exemplo: `/{project} explique este projeto`."
        )
        lines.extend(
            " · ".join(f"/{alias}" for alias in aliases[index : index + 6])
            for index in range(0, len(aliases), 6)
        )
        lines.append(
            "Você pode combinar agente e projeto: `/AGENTE /PROJETO TAREFA`. Use /new para manter uma conversa própria em outro projeto."
        )
    covered = {*RESERVED_CHAT_COMMANDS, *engines, *aliases}
    lines.extend(
        f"/{command['command']} — {command['description'][:200]}"
        for command in build_bot_commands(
            cfg.runtime,
            include_file=cfg.files.enabled,
            include_topics=cfg.topics.enabled,
        )
        if command["command"] not in covered
    )
    # Leave room below Telegram's UTF-16 limit; large project lists span messages.
    messages: list[str] = []
    current = ""
    for line in lines:
        # Telegram's Markdown renderer otherwise merges adjacent command lines.
        if line:
            line += "  "
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-16-le")) // 2 > 3500:
            messages.append(current.rstrip())
            current = line
        else:
            current = candidate
    if current.strip():
        messages.append(current.rstrip())
    return messages


async def handle_help(cfg: TelegramBridgeConfig, msg: TelegramIncomingMessage) -> None:
    reply = make_reply(cfg, msg)
    for text in build_help_messages(cfg):
        await reply(text=text)
