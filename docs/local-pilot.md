# Anexos com legenda

A instalação local recebe arquivos e fotos com a tarefa na legenda para Codex,
Claude e Agy. O anexo fica em `incoming/` no projeto da conversa; o agente recebe
a legenda e o caminho para ler com suas ferramentas. Álbuns geram uma única
tarefa; anexos sem legenda são apenas salvos. O limite é 20 MiB por arquivo.

O gerador `scripts/configure_local.py` habilita `files.enabled`, `files.auto_put`
e `files.auto_put_mode = "prompt"`, com acesso restrito ao dono configurado.
Instalações existentes precisam dessas mesmas opções e de reinício do bot.
Detalhes e limites em [file-transfer.md](how-to/file-transfer.md).

Validação em 30/10/2026: **672 testes passaram**, cobertura **82,37%**; Ruff
aprovado e Ty sem erros, com cinco avisos preexistentes. Os testes de anexos
verificam legenda, álbum, retomada da sessão e diretório do tópico nos três
agentes, além do envio sem legenda e do aviso de recurso desativado.
Em três chamadas reais pelo ai-memory, Codex, Claude e Agy leram arquivos de
teste a partir dos caminhos recebidos no prompt e devolveram corretamente o
conteúdo. Evidência privada: `~/.local/share/takopi-test-project/validation/attachment-results.json`.
O download/roteamento do Telegram foi testado com API simulada; o envio real
pelo usuário deve ser repetido depois da ativação.

# Telemetria nas respostas

O rodapé com modelo, esforço, contexto e cotas de 5 horas/semanal está descrito
em [telegram-status.md](telegram-status.md), incluindo as limitações por agente.

# Expansão para Windows, outro WSL e Antigravity

A expansão de 28/10/2026, incluindo `/new agy`, os 32 projetos e os históricos
importados, está documentada em [migration-20261029.md](migration-20261029.md).
Essa atualização complementa o registro do piloto abaixo.

# Piloto local: Codex e Claude pelo Telegram

Este checkout adapta o Takopi 0.23.4 (upstream e0123d4f9b2588bf45ec7250ad648c324e20ce20). A primeira etapa usa apenas o WSL Ubuntu-26.04. Nenhum executor remoto ou Agy foi configurado.

Em 28/10/2026, o bot foi conectado ao grupo MyBots, com acesso restrito ao usuário identificado pelo `/setup`. Após os testes reais de ambos os agentes, o serviço ficou ativo e habilitado no systemd do usuário, com linger. Há 27 projetos/diretórios de trabalho cadastrados e 40 históricos locais no catálogo. O WSL precisa estar em execução.

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

- `/help`: mostra o guia em português de todos os comandos, com exemplos e os atalhos dos projetos cadastrados. Responde no mesmo chat/tópico, sem executar um agente, e informa recursos desativados.
- `/new`: escolhe Codex ou Claude por botões.
- `/new codex` ou `/new claude`: escolhe o projeto por botões.
- `/new claude projeto nome da conversa`: cria diretamente um tópico independente.
- `/projects`: lista os projetos cadastrados.
- `/sessions`, `/sessions codex` ou `/sessions claude`: lista as conversas do bot com links e os históricos locais ainda sem tópico. Use `/sessions all 2` para a página seguinte.
- `/resume codex ID` ou `/resume claude ID`: abre um histórico local no seu projeto, em um tópico. Copie o comando mostrado por `/sessions`. Se já existe um tópico para o ID, retorna seu link. A primeira mensagem no tópico retoma o CLI através do ai-memory; listar e abrir um tópico não executa uma tarefa de modelo.
- Escreva no tópico para iniciar/retomar a conversa. A primeira mensagem inicia o CLI; criar um tópico não consome uma chamada de modelo.
- `/model set MODELO` e `/reasoning set NÍVEL`: usam os controles do Takopi para aquela conversa. Sem override, o CLI mantém os padrões do usuário.
- `/ctx`: mostra projeto/contexto; `/agent` mostra o agente. Mantenha cada tópico associado ao projeto original.
- `/stop` dentro do tópico interrompe sua tarefa ativa; o botão de cancelamento da mensagem de progresso também funciona. A fila pode ser cancelada respondendo à mensagem de progresso específica com `/cancel`.

O histórico fica salvo após reiniciar o bot. `/new` no modo deste piloto cria outro tópico e preserva a conversa anterior. Duplo clique na mesma seleção não cria dois tópicos enquanto o estado local estiver preservado. Permissões por ID numérico são aplicadas a mensagens e callbacks.

## ai-memory e permissões

Cada conversa nova recebe um workstream `tg-<uuid>`. O mapeamento entre ID nativo, projeto, diretório de configuração e workstream é persistido em `~/.takopi/ai_memory_sessions.sqlite3`. Na retomada o mesmo vínculo é obrigatório. Trocar o diretório/configuração da sessão falha explicitamente, sem criar outra conversa silenciosamente.

O processo passa por `ai-memory run --require-server --no-autowire`. Codex usa `exec --json --yolo`; Claude usa `-p --output-format stream-json --verbose --permission-mode auto`, sem acrescentar as regras amplas de ferramentas que o Takopi original fornece. Prompts seguem por stdin. O resultado final só é entregue depois que o launcher termina, para considerar falhas de captura/finalização. A adoção explícita via `/resume` autoriza apenas aquele ID, diretório e configuração; a primeira execução cria o workstream com o seletor nativo de retomada, e as seguintes usam o vínculo salvo.

Claude informa seu modo efetivo no evento inicial; o adaptador interrompe a execução se não for `auto`. Auto pode negar ações e pedir intervenção: o piloto não troca silenciosamente para bypass. O caminho não interativo não reproduz todos os diálogos da TUI, inclusive perguntas estruturadas. Essas situações devem aparecer como erro/solicitação na conversa e ser verificadas no teste de uso.

## Limites desta etapa

- Com `topics.import_native_sessions = true`, `/sessions` inclui o índice e os rollouts do Codex e os históricos de `~/.claude/projects`. Não há anexação a um CLI que esteja executando no terminal: a adoção/retomada é bloqueada conservadoramente quando há um processo nativo no mesmo diretório. Encerre o CLI no terminal antes de alternar para o Telegram.
- O envio da inicialização e o menu foram confirmados pela API real do Telegram. A validação de conversas iniciadas por mensagens do usuário no grupo é uma etapa separada dos testes locais.
- O Takopi descarta mensagens acumuladas enquanto estava desligado ao iniciar. Isso evita executar tarefas antigas automaticamente; reenvie a tarefa desejada após o aviso de inicialização.
- Áudio permanece desativado. Anexos com legenda foram habilitados em 30/10/2026,
  conforme a seção no início deste documento. Regras/skills/MCPs continuam sendo
  os dos CLIs.
- `plugins.enabled = ["takopi"]` autoriza a distribuição Python que fornece os plugins. `enabled_engines = ["codex", "claude"]` limita os executores disponíveis, mesmo que outros CLIs estejam instalados. O transporte é Telegram. Sem `enabled_engines`, o comportamento upstream de descobrir todos os executores continua disponível; valores desconhecidos ou exclusão do agente padrão são rejeitados.
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

## Ativação em 28/10/2026

O token foi fornecido por arquivo privado. A API confirmou que MyBots é um supergrupo com tópicos, que o bot é administrador com permissão de gerenciar tópicos e que o autor das mensagens `/setup` é o criador do grupo. Esse usuário foi incluído como único operador autorizado. O ID positivo que acompanha o ID `-100…` do grupo não foi confundido com um ID de usuário.

A primeira inicialização falhou com `Unknown engine 'codex'`: a configuração anterior usava IDs de executores em `plugins.enabled`, que aceita nomes de distribuições Python. A configuração e o gerador foram corrigidos; a seleção explícita de executores ficou em `enabled_engines`. O teste de configuração agora constrói o runtime real pelos entry points instalados e confirma que apenas Codex e Claude estão disponíveis.

Verificações após a correção:

- `takopi doctor`: token, grupo, tópicos e configuração aprovados.
- Serviço `takopi.service`: ativo; menu confirmado por `getMyCommands` e envio da inicialização registrado como `startup.sent`. Configuração privada em `~/.takopi/takopi.toml`, modo 0600. Cópia anterior preservada no mesmo diretório privado.
- Suíte completa: **614 testes passaram**, cobertura **81,74%**. Ruff lint/formatação aprovados; Ty sem erros, mantendo os cinco avisos preexistentes.
- Validação humana pendente: enviar `/new codex teste` e `/new claude teste`, conversar nos tópicos, verificar `/sessions`, isolamento, cancelamento e retomada após reinício. Não houve nova execução de modelo automatizada nesta ativação; os testes reais dos CLIs estão registrados acima.

Depois dessa validação, cadastrar os projetos desejados e habilitar o serviço no início do WSL. As outras máquinas e o Agy continuam para uma etapa posterior.

## Testes do usuário e ampliação para projetos locais

O usuário testou os dois agentes no Telegram. Os logs confirmaram conclusão com sucesso e saída zero: Claude em aproximadamente 7,3 segundos e Codex em 6,9 segundos. Cada tópico recebeu um ID nativo e um workstream distintos. A leitura do ledger do ai-memory confirmou a pergunta e a resposta de cada conversa, além do checkpoint com saída zero. As flags exigidas foram confirmadas na linha de lançamento.

Depois de confirmar que não havia execução de agente ativa, foi criada uma cópia privada de configuração, estados JSON e banco SQLite em `~/.takopi/backups/validation-20261028T053730Z`. O serviço foi habilitado (`systemctl --user enable takopi`), o linger do usuário ativado e o serviço reiniciado. Os dois tópicos e os dois vínculos nativos permaneceram idênticos; o novo processo enviou a inicialização sem falhas. Isso valida a persistência do estado, mas ainda não representa um novo turno de conversa enviado pelo usuário após o reinício.

O usuário pediu para incluir todos os projetos e sessões deste WSL. Foram cadastrados os diretórios existentes encontrados nos históricos locais, mais o próprio Takopi, sem duplicar caminhos que diferem apenas na capitalização de `Users/SMinato`. O reload da configuração e o novo menu foram confirmados nos logs.

O inventário privado `~/.takopi/native-session-inventory.json` contém 20 históricos do Codex e 20 do Claude, obtidos de `~/.codex/state_5.sqlite` e `~/.claude/projects/*/*.jsonl`. Uma sessão aponta para um bind mount temporário do Docker que já não existe; ela foi preservada no inventário e não teve seu diretório substituído por suposição.

**Pendência de implementação:** incluir esses históricos no `/sessions` e permitir abrir/retomar cada um em seu tópico. O adaptador atual exige vínculo prévio no `ai_memory_sessions.sqlite3`, portanto não basta copiar IDs para o estado de tópicos. É necessário validar a adoção de uma sessão nativa existente por um novo workstream, conservar cwd e configuração originais, evitar sessões que estejam em execução e testar a retomada de ambos os agentes. O inventário não significa que os históricos já foram importados ou que podem ser retomados pelo Telegram.

O cadastro atual de projetos e as duas sessões criadas pelo bot estão operacionais. A expansão para outras instalações e Agy permanece fora desta etapa.

O desenvolvimento foi interrompido novamente quando a consulta indicou 9% restantes na janela de 5 horas e 52% na semanal. A varredura adicional de repositórios sob `projects`, `Desktop` e `Documents` não terminou; repetir essa busca na continuidade antes de afirmar que todos os projetos do disco foram encontrados. O cadastro concluído cobre os diretórios existentes identificados nos históricos locais e o Takopi. O inventário privado permite retomar a implementação da importação sem reler o conteúdo completo das conversas.

## Continuação concluída após o pedido do usuário

A varredura adicional terminou e o usuário pediu para continuar. Foram cadastrados todos os projetos de trabalho encontrados nos históricos deste WSL e nas árvores pesquisadas, incluindo os repositórios de Documents. Um repositório instalado dentro de `Documents/PowerShell/Modules/z/1.1.14` foi identificado como dependência e não cadastrado como projeto de trabalho. O catálogo final contém:

| Alias | Diretório relativo a `/mnt/c/Users/SMinato` |
| --- | --- |
| sminato | raiz |
| llamp | projects/llamp |
| gpsmock | projects/gpsmock |
| chessrush | desktop/ChessRush |
| takopi | projects/takopi |
| dashjob | Documents/Projetos/DashJob |
| preenchedoria | Documents/Projetos/PreenchedorIA |
| sistema_estoque | Documents/Projetos/Projeto-SistemaEstoque-Full-Stack |
| yidle | Documents/Projetos/Yidle |
| flstudio_mcp | Documents/Projetos/audio/flstudio-mcp |
| portfolio | Documents/Projetos/sminato27-portfolio |
| telegram_upload | Documents/Projetos/telegram-upload |
| telegram_drive | Documents/Scripts/Telegram-Drive |
| upload_assistant | Documents/Scripts/Upload-Assistant |
| flutter | Documents/flutter |
| ctb_backup | Documents/Projetos/BKPChaturbate-dvr_working020726 |
| nelio_aulas | Documents/Projetos/NelioAulas |
| audio | Documents/Projetos/audio |
| govt_doorway | Documents/Projetos/govt-doorway-connect-main |
| untitled | Documents/Projetos/untitled |
| hellyes | Documents/Scripts/HellYes |
| opus_tools | Documents/Scripts/OpusTools |
| tdarr_scripts | Documents/Scripts/Tdarr |
| votify | Documents/Scripts/votify-main |
| power_toggle | Documents/Scripts/PowerToggle |
| scripts | Documents/Scripts |

O projeto `teste` permanece em `~/.local/share/takopi-test-project`, totalizando 27 aliases. A conferência final incluiu projetos sem Git, um diretório de aulas Java, a área de áudio e pastas com scripts próprios; diretórios contendo apenas aplicações binárias/dependências não receberam aliases individuais, mas continuam acessíveis pelo projeto `scripts` quando estão nessa árvore. O bot confirmou o novo menu e `/resume` pela API real do Telegram depois de reiniciar, com serviço ativo e habilitado, sem reinicializações por falha.

O catálogo nativo é atualizado a cada `/sessions`: 20 históricos do Codex e 20 do Claude na validação, sem duplicar os dois IDs já vinculados a tópicos. O histórico com diretório Docker desaparecido continua visível, com indicação de indisponibilidade, sem tentar mudar seu projeto. Históricos antigos criam tópicos sob demanda, evitando criar dezenas de tópicos durante o cadastro. Não foram alterados os bancos e arquivos de histórico dos CLIs para construir a listagem.

Testes reais de adoção: dois históricos do projeto isolado, anteriores ao bot, foram retomados pelo adaptador com seus IDs originais. Ambos recuperaram o código memorizado e finalizaram com saída zero; evidências privadas em `~/.local/share/takopi-test-project/validation/import-results.json` e `import-validation.log`. Foram usados os CLIs reais e `ai-memory run`, sem repetir automaticamente tarefas antigas de projetos reais.

A suíte completa passou com **619 testes**, cobertura **81,61%**. Um aviso de fechamento de conexão no fixture SQLite novo foi corrigido e os testes focados repetidos. Ruff e formatação passaram; Ty permanece sem erros, com cinco avisos preexistentes. A interação de `/resume` no Telegram usa o fluxo testado de criação de tópicos; os novos testes cobrem listagem, reutilização de tópico, recusa de diretório ausente/ocupado e adoção seguida de retomada. O próximo uso natural pode ser `/sessions claude`, seguido do `/resume` correspondente e de uma mensagem no tópico.
