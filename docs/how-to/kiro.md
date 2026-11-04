# Kiro no Telegram

O adaptador `kiro` usa o Kiro CLI 3.0.0 instalado no WSL, com o motor V3
explicitamente selecionado. Ele reutiliza o login e as configurações locais
do Kiro, recebe o prompt por stdin e acompanha a saída `stream-json`.

Depois de instalar o Kiro e entrar com `kiro-cli login`, reinstale o Takopi
para registrar o novo backend. Em um ambiente Python já preparado:

```bash
python -m pip install --no-deps -e .
```

Inclua `kiro` na lista `enabled_engines` existente em `~/.takopi/takopi.toml`:

```toml
enabled_engines = ["codex", "claude", "agy", "kiro"]

[kiro]
trust_all_tools = false
# model = "auto"
# effort = "low"
# agent = "meu-agente"
```

O padrão preserva as permissões do Kiro. No modo sem interação, uma ferramenta
que exige confirmação é recusada. Para operação autônoma, o operador pode
definir `trust_all_tools = true`: o Takopi adiciona `--trust-all-tools` apenas
ao processo filho, sem alterar permissões globais do Kiro. Os projetos
cadastrados são tratados como workspaces confiáveis nessa execução.

Reinicie o bot após a instalação/configuração. No Telegram:

- `/new kiro PROJETO` cria um tópico independente.
- `/kiro PROMPT` executa uma tarefa com o Kiro.
- `/model set kiro MODELO` e `/reasoning set kiro NÍVEL` alteram a seleção local.
- `/sessions kiro` lista as conversas criadas pelo bot.
- `/stop` cancela a tarefa ativa e encerra seu grupo de processos.

Os níveis expostos são `none`, `low`, `medium`, `high`, `xhigh` e `max`;
o Kiro valida quais o modelo escolhido aceita. `auto` pode não oferecer esforço.
Consulte os modelos disponíveis com `kiro-cli chat --list-models --format json`.

Arquivos com legenda e álbuns seguem o mesmo fluxo dos outros agentes: são
salvos em `incoming/` no projeto e seus caminhos entram no prompt. A retomada
usa o ID exato da conversa (`kiro-cli chat --resume-id ID`), inclusive após
reiniciar o bot. Históricos do Kiro iniciados fora do Takopi ainda não são
importados pelo catálogo `/sessions`; os históricos dos outros agentes são
preservados. Este adaptador executa o CLI diretamente, sem o launcher
`ai-memory run`.

O rodapé recebe modelo, esforço e percentual de contexto emitidos pelo Kiro.
Não se converte esse percentual em uma contagem fictícia de tokens. As cotas
de 5h/semanal aparecem como indisponíveis, e `/cotas` explica que o Kiro não
informa essas janelas neste modo. O consumo de créditos de um turno não é
tratado como saldo da conta.

Somente `runFinished`/`runError` encerram a resposta; eventos intermediários
de fim de turno não encerram o processo. Replay de histórico e pensamentos
não entram na resposta. Erros, interrupções, respostas vazias e IDs de sessão
divergentes não são considerados sucesso. Não há reenvio automático do prompt.

Referências oficiais: [modo headless](https://kiro.dev/docs/cli/headless/)
e [eventos e retomada do V3](https://kiro.dev/docs/cli/v3/acp-migration/).

Validação em 04/11/2026: 784 testes passaram, cobertura de 83,27%, Ruff
aprovado e Ty sem erros (cinco avisos preexistentes). Duas chamadas reais
confirmaram leitura de arquivo, retomada do mesmo ID e memória da conversa,
troca de modelo, esforço `low` e percentual de contexto. A tentativa de escrita
sem liberação de ferramentas foi corretamente recusada pelo Kiro. Telegram,
anexos, cancelamento e falhas do protocolo foram verificados com simulações.

## Memória compartilhada com Claude e Codex (ai-memory)

O `kiro-cli` participa do mesmo `ai-memory` usado por Claude e Codex, mas por um
caminho diferente do `AiMemoryConfig`/`ai_memory = true` deste repositório (que
o adaptador Kiro recusa de propósito — ver `build_runner`). A integração é
nativa do `kiro-cli` em si, feita uma vez por máquina, fora do takopi:

```bash
ai-memory install-mcp --client kiro-cli --apply       # ~/.kiro/settings/mcp.json
ai-memory install-hooks --agent kiro-cli-v3 --apply   # ~/.kiro/hooks/ai-memory.json
```

Isso dá a toda sessão `kiro-cli` (via takopi ou não) os hooks de ciclo de vida
(`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`) e o
MCP do `ai-memory`, iguais aos que Claude e Codex já tinham. Confirme com
`ai-memory doctor` na raiz do projeto: as três linhas (`claude-code`, `codex`,
`kiro-cli`) devem aparecer capturadas.

## Pipeline de subagentes para modelos Anthropic (`claude-pipeline`)

Quando o modelo selecionado para o Kiro começa com `claude` (ex.: `claude-opus-5.5`,
`claude-sonnet-5.5`) e nenhum `kiro.agent` está configurado, o `build_args`
seleciona automaticamente `--agent claude-pipeline`. Esse agente — e os nove que
ele delega (`search`, `grep`, `docs`, `explorer`, `planner`, `coder`, `tester`,
`reviewer`, `consultant`) — são uma exportação do pipeline de subagentes e das
regras de eficiência do `~/.claude/CLAUDE.md` deste operador, adaptados ao
esquema de agente do Kiro CLI V3.

Esses dez arquivos vivem em `~/.kiro/agents/*.json`, **fora deste repositório**
(config local da máquina, não versionada). Um modelo não-Anthropic continua
usando o agente padrão do Kiro, sem pipeline nenhum. Um `kiro.agent` explícito
no `takopi.toml` sempre tem prioridade sobre essa seleção automática.

Duas diferenças conhecidas em relação ao `CLAUDE.md` original:

- **`explorer` não tem Bash.** O original tinha um Bash somente-leitura com
  lista de comandos permitidos, via hook do Claude Code. O hook `PreToolUse`
  do Kiro CLI não expõe qual agente está chamando a ferramenta, e uma regra de
  permissão específica somada a uma regra genérica de bloqueio faz a ferramenta
  de shell desaparecer por completo em vez de aplicar a lista — testado e
  descartado. `explorer` ficou só com leitura e busca.
- **`tester` e `reviewer` têm `execute_bash` liberado e `fs_write` negado**, mas
  a negação de `fs_write` não cobre escrita feita via shell (`echo > arquivo`,
  por exemplo) — é uma limitação do shell em si, não desta configuração. Como
  no `CLAUDE.md` original, "não corrige/edita código" para esses dois é uma
  convenção do prompt do agente, não uma parede técnica.
