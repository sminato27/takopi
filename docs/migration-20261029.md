# Migração para Ubuntu-26.04 e Antigravity

Executada em 28/10/2026 à noite (29/10 UTC). O bot foi atualizado e reiniciado.

## Uso

```text
/new agy
/new agy llamp
/sessions agy
/sessions claude
/sessions all 2
/projects
/help
```

`agy` é o Antigravity. `/resume AGENTE ID` abre uma conversa importada em um
tópico. Os modelos e o esforço podem ser definidos por `/model` e `/reasoning`.
Os três agentes continuam iniciando por `ai-memory run --require-server
--no-autowire`. Codex mantém `--yolo`, Claude mantém `--permission-mode auto`, e
Agy usa `--dangerously-skip-permissions`. O adaptador verifica o modo informado
pelo evento inicial do Agy (`always-proceed`).

## Dados preservados

| Origem | Histórico importado |
| --- | --- |
| WSL Ubuntu — Claude | 26 conversas; mais um arquivo órfão sem conversa reconhecível |
| Windows — Claude | 61 conversas |
| WSL Ubuntu — Antigravity | 24 conversas indexadas, bancos e artefatos associados |
| Windows — Antigravity | Um banco sem passos e sem entrada no índice, preservado no backup |
| Codex de ambas as origens | Índices vazios; nenhum histórico adicional encontrado |

O catálogo final tem 20 conversas Codex, 107 Claude e 26 Agy, incluindo duas
conversas de teste do Agy. Destes históricos, 20/96/20, respectivamente, têm
arquivo e projeto disponíveis. Os demais continuam visíveis, mas sua retomada
é recusada por falta do diretório ou do arquivo. Uma conversa Agy não informa
workspace. Um CLI ativo no mesmo diretório também impede a adoção.

Há 32 projetos cadastrados. Os 27 anteriores foram mantidos, acrescentando:

| Atalho | Diretório |
| --- | --- |
| `wsl_home` | `/home/sminato` |
| `rclone` | `/mnt/c/Users/SMinato/Documents/rclone` |
| `tdl_windows` | `/home/sminato/projects/tdl-windows` |
| `chessrush_ghidra` | `/home/sminato/chessrush-ghidra` |
| `chessrush_ghidra_ubuntu` | `/home/sminato/migrated/ubuntu/chessrush-ghidra` |

Projetos no C: já acessíveis pelo WSL continuam nas pastas compartilhadas. O
worktree TDL tinha `.git` apontando para `C:/...`, incompatível com Git no Linux.
Foi criado um repositório independente, com o mesmo HEAD, índice, arquivos e
alterações pendentes. Foram comparados os hashes de 607 arquivos e o resultado
de `git status --porcelain`. A cópia Ghidra do outro WSL foi mantida separada:
um banco diferia da versão já presente nesta instância.

`S23`, `norden`, `ScrappyCoco`, o antigo `Documents/PreenchedorIA`, os scratch
workspaces do Claude Desktop, os áudios antigos e os projetos do antigo E: não
estavam nos caminhos registrados. `/mnt/e` no outro WSL era uma pasta vazia.
Não foram criadas pastas vazias para aparentar que esses projetos existem.

## Migração e configuração

Os originais não foram removidos. Snapshot, bancos SQLite consistentes,
manifestos, hashes e configurações anteriores estão em:

```text
/home/sminato/.local/share/takopi-migration/20261029/
```

Esse diretório contém dados privados e autenticação do Agy; não deve ser
versionado ou compartilhado. O executável Linux Agy 1.4.9 e sua configuração
foram copiados do WSL Ubuntu. O MCP ai-memory aponta para o servidor local.

`scripts/migrate_claude_histories.py` faz uma simulação por padrão e só copia
com `--apply`. Não substitui arquivos divergentes. Converte apenas os campos
estruturais `cwd` e `projectPath`; preserva os textos, IDs, subagentes e anexos.
`--cwd-map` aceita um JSON de caminhos confirmados de projetos copiados, usado
para o TDL. Índices antigos com caminhos da origem não são copiados.

Os oito conflitos iniciais eram arquivos `memory/MEMORY.md`; a versão local
foi mantida. Agentes, regras, modelos, hooks e autenticação atuais do Codex e
Claude foram preservados. A única alteração global do Claude foi
`cleanupPeriodDays: 3650`, com backup da configuração anterior. Três históricos
de setembro desapareceram após a inicialização do Claude e foram restaurados.
Esse comportamento é compatível com a limpeza em segundo plano de históricos
antigos, cujo padrão é 30 dias. A retenção longa evita essa expiração dos
históricos importados. [Referência oficial de retenção](https://code.claude.com/docs/en/settings-reference#cleanupperioddays).

Para habilitar Agy em outra instalação já configurada:

```toml
enabled_engines = ["codex", "claude", "agy"]

[agy]
ai_memory = true
dangerously_skip_permissions = true
```

É necessário instalar/autenticar `agy`, configurar o ai-memory e reinstalar o
pacote Takopi após adicionar seu entry point. O script `configure_local.py`
continua criando o piloto inicial de Codex/Claude; `run_local.py` aceita Agy
opcional e exige ai-memory para todos os agentes habilitados.

## Verificação e limites

- 639 testes passaram; cobertura de 82,04%. Ruff passou em `src`, `tests` e nos
  scripts alterados. Ty não apresentou erros; permanecem cinco avisos anteriores.
- Agy: criação real, retomada após reconstruir o runner e lembrança de um código
  da primeira mensagem; ID preservado. Uma conversa antiga de DashJob também
  foi retomada e respondeu com sucesso.
- ai-memory: workstream Agy vinculado ao mesmo ID nativo em ambos os turnos.
  O ledger recebeu checkpoints, mas **não recebeu o texto das mensagens** no
  teste em modo print. O diálogo continua salvo e retomável no Agy. A captura
  completa no ai-memory permanece uma limitação desta combinação de versões.
- Claude: reconheceu o ID importado e iniciou em `auto`, mas a inferência foi
  recusada pelo limite de uso de cinco horas. A resposta após a migração precisa
  ser validada quando a cota voltar. A falha não substituiu nem apagou o histórico.
- O bot foi reiniciado sem trabalhos ativos. `getMyCommands` confirmou 48
  comandos, `/help` primeiro, `/agy` e os novos projetos. Serviço ativo/habilitado.
- A nova interação pelo aplicativo Telegram ainda depende do teste do usuário;
  os comandos e o roteamento foram cobertos por testes automatizados. Não foram
  executados builds de todos os projetos migrados.

Para reverter somente a configuração do bot, restaure `takopi-before.toml` no
diretório de backup para `~/.takopi/takopi.toml` e reinicie `takopi.service` quando
não houver tarefas ativas. Os históricos e cópias de projetos podem permanecer;
não restaure snapshots sobre conversas que receberam mensagens novas.
