# Piloto local: Codex e Claude pelo Telegram

Este checkout adapta o Takopi 0.23.4 (upstream e0123d4f9b2588bf45ec7250ad648c324e20ce20). A primeira etapa usa apenas o WSL Ubuntu-26.04. Nenhum executor remoto ou Agy foi configurado.

## Instalação reproduzível

Requer Python 3.14, uv, os CLIs autenticados e ai-memory com hooks/MCP já configurados. Instale as dependências fixadas no lockfile:

```bash
UV_PROJECT_ENVIRONMENT=/home/sminato/.local/share/takopi-venv uv sync --frozen --group dev
```

O serviço usa essa instalação editável e o mesmo usuário dos CLIs. Preserva `~/.codex`, `~/.claude`, agentes, regras e skills; entra no diretório do projeto selecionado antes de iniciar a execução. Não usa `--bare`, não copia credenciais e não instala hooks durante tarefas do Telegram.

## Bot dedicado e grupo

Crie um bot no @BotFather, um grupo privado com tópicos e adicione o bot como administrador com **Gerenciar tópicos**. Guarde o token em um arquivo privado fora deste repositório. Não o passe como argumento de terminal.

Envie `/setup` no grupo e descubra os IDs:

```bash
/home/sminato/.local/share/takopi-venv/bin/python scripts/configure_local.py --token-file /caminho/privado/token --discover
```

Confira seu próprio ID na saída e configure, substituindo os números:

```bash
/home/sminato/.local/share/takopi-venv/bin/python scripts/configure_local.py \
  --token-file /caminho/privado/token --chat-id -1001234567890 --owner-id 123456789 \
  --project exemplo=/caminho/do/projeto
```

O script valida o grupo, as permissões do bot e a associação do dono. Salva `~/.takopi/takopi.toml` com permissão 0600 e recusa sobrescrever configuração existente. O projeto `teste` fica disponível sem precisar cadastrar projetos reais. O token nunca aparece na saída do script.

O arquivo de serviço em `deploy/takopi.service` é específico desta instalação. Depois de instalado em `~/.config/systemd/user/takopi.service` e executado `systemctl --user daemon-reload`:

```bash
systemctl --user start takopi
systemctl --user status takopi
journalctl --user -u takopi -n 60 --no-pager
systemctl --user stop takopi
```

Só habilite o início automático depois de validar o piloto: `systemctl --user enable takopi`. WSL precisa estar em execução; isso não liga o PC nem inicia uma distribuição encerrada.

## Uso

- `/new`: escolhe Codex ou Claude por botões.
- `/new codex` ou `/new claude`: escolhe o projeto por botões.
- `/new claude projeto nome da conversa`: cria diretamente um tópico independente.
- `/projects`: lista os projetos cadastrados.
- `/sessions`, `/sessions codex` ou `/sessions claude`: lista as conversas criadas neste bot com links. Usa `/sessions all 2` para a página seguinte.
- Escreva no tópico para iniciar/retomar a conversa. A primeira mensagem inicia o CLI; criar um tópico não consome uma chamada de modelo.
- `/model set MODELO` e `/reasoning set NÍVEL`: usam os controles do Takopi para aquela conversa. Sem override, o CLI mantém os padrões do usuário.
- `/ctx`: mostra projeto/contexto; `/agent` mostra o agente. Mantenha cada tópico associado ao projeto original.
- `/stop` dentro do tópico interrompe sua tarefa ativa; o botão de cancelamento da mensagem de progresso também funciona. A fila pode ser cancelada respondendo à mensagem de progresso específica com `/cancel`.

O histórico fica salvo após reiniciar o bot. `/new` no modo deste piloto cria outro tópico e preserva a conversa anterior. Duplo clique na mesma seleção não cria dois tópicos enquanto o estado local estiver preservado. Permissões por ID numérico são aplicadas a mensagens e callbacks.

## ai-memory e permissões

Cada conversa nova recebe um workstream `tg-<uuid>`. O mapeamento entre ID nativo, projeto, diretório de configuração e workstream é persistido em `~/.takopi/ai_memory_sessions.sqlite3`. Na retomada o mesmo vínculo é obrigatório. Trocar o diretório/configuração da sessão falha explicitamente, sem criar outra conversa silenciosamente.

O processo passa por `ai-memory run --require-server --no-autowire`. Codex usa `exec --json --yolo`; Claude usa `-p --output-format stream-json --verbose --permission-mode auto`, sem acrescentar as regras amplas de ferramentas que o Takopi original fornece. Prompts seguem por stdin. O resultado final só é entregue depois que o launcher termina, para considerar falhas de captura/finalização.

Claude informa seu modo efetivo no evento inicial; o adaptador interrompe a execução se não for `auto`. Auto pode negar ações e pedir intervenção: o piloto não troca silenciosamente para bypass. O caminho não interativo não reproduz todos os diálogos da TUI, inclusive perguntas estruturadas. Essas situações devem aparecer como erro/solicitação na conversa e ser verificadas no teste de uso.

## Limites desta etapa

- `/sessions` lista tópicos e conversas do bot, não importa sessões anteriores abertas nos terminais. Não há anexação a uma sessão de terminal que já esteja executando.
- Sem token/grupo não há teste real da entrega no Telegram; testes locais usam um transporte simulado.
- O Takopi descarta mensagens acumuladas enquanto estava desligado ao iniciar. Isso evita executar tarefas antigas automaticamente; reenvie a tarefa desejada após o aviso de inicialização.
- Áudio e transferência de arquivos ficam desativados no piloto. Regras/skills/MCPs continuam sendo os dos CLIs.
- A configuração proposta mantém apenas os plugins de Codex, Claude e Telegram disponíveis.
- Para voltar ao uso normal dos CLIs, basta parar o serviço. Nenhuma configuração global dos agentes foi substituída.

## Verificação

```bash
/home/sminato/.local/share/takopi-venv/bin/pytest
/home/sminato/.local/share/takopi-venv/bin/ruff format --check src tests
/home/sminato/.local/share/takopi-venv/bin/ruff check src tests
/home/sminato/.local/share/takopi-venv/bin/ty check src tests
```

Os testes adicionais verificam passagem real pelo subprocesso do launcher, criação/retomada, configuração/cwd, isolamento de conversas, finalização, falha de permissão, seleção de projetos, persistência de tópicos e repetição de callbacks. Os resultados de testes reais dos CLIs devem ser registrados junto à entrega.

## Estado da validação em 27/10/2026

- Instalação editável concluída em `~/.local/share/takopi-venv` com dependências do lockfile. Serviço instalado e validado pelo systemd, ainda **inativo e desabilitado** porque faltam token e grupo.
- Suíte completa: **608 testes passaram**, cobertura **81,65%** (mínimo exigido: 81%). Depois foram adicionados três testes de configuração, executados separadamente: **3 passaram**. Nenhum código de produção mudou após a suíte completa.
- Ruff lint e formatação: aprovados em todos os fontes/testes e nos três scripts locais. Ty: sem erros, cinco avisos já existentes no commit upstream, confirmados em uma extração limpa desse commit.
- Teste real `scripts/smoke_local.py`: Codex e Claude criaram uma sessão e a retomaram com o mesmo ID, recuperando um código da conversa anterior. Quatro execuções concluídas com sucesso, todas pelo ai-memory, com confirmação de captura de eventos. Claude iniciou em `auto`; Codex recebeu `--yolo`.
- Evidências locais: `~/.local/share/takopi-test-project/validation/results.json`, `smoke.log` e `pytest.log`. O ai-memory executado no teste informou versão **3.2.8**. O Claude também emitiu `rate_limit_event`, ainda ignorado pelo parser upstream; isso não impediu as respostas nem a retomada.
- Nenhum teste real de API/entrega no Telegram foi realizado. Os testes de tópicos, autorização, cancelamento e configuração usaram transporte/API simulados.

### Continuidade

O desenvolvimento foi interrompido ao consultar **9% restantes na janela de 5 horas do Codex** e 67% na semanal, seguindo a regra de preservação do usuário. A telemetria não fornece uma janela diária separada. O projeto `default/banteg-takopi`, derivado do remote, ainda não estava cadastrado no ai-memory; por isso este registro de continuidade fica no repositório.

Próximos passos:

1. Receber o caminho local do arquivo com o token de um bot dedicado. O usuário deve adicioná-lo como administrador a um grupo privado com tópicos e enviar `/setup` ali; `configure_local.py --discover` identifica grupo e usuário sem revelar o token.
2. Confirmar pelos dados do usuário quais projetos além de `teste` cadastrar. Gerar a configuração privada com `configure_local.py`, preservando regras/credenciais atuais dos CLIs.
3. Iniciar o serviço e validar no Telegram `/new codex`, `/new claude`, seleção de projeto, envio e retomada, `/sessions claude`, `/stop`, isolamento entre tópicos e retomada após reinício do bot.
4. Só após validação pelo usuário, habilitar início automático e planejar as outras instalações/Agy. Não expandir o escopo neste piloto.

Não há token nem credenciais no repositório. `~/.takopi/takopi.toml` ainda não foi criado. As configurações globais de Codex e Claude não foram alteradas. `/sessions` continua limitado às conversas deste bot; importação de históricos do terminal permanece fora desta etapa.
