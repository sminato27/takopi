# Estado do agente em cada resposta

Codex, Claude e Antigravity mostram automaticamente um rodapé nas respostas do
Telegram. Não é necessário habilitar uma opção nem enviar um comando de consulta.

```text
Modelo: gpt-6-astra · Effort: xhigh
Contexto: 39% usado (101.3k/258.4k)
Cotas · 5h: 17% usado · renova em 2h 10min
Semanal: 33% usado · renova em 4d 3h
```

Os números acima são ilustrativos. As cotas representam **uso**, não saldo, e
pertencem à conta/grupo de modelos, não apenas ao tópico. O limite chamado de
“diário” nesta instalação é a janela de **5 horas**. A renovação é calculada a
partir do horário informado pelo provedor, no momento em que a mensagem é
renderizada. Mensagens antigas continuam sendo registros daquele instante.

O progresso recebe novos valores quando o CLI emite dados. A resposta final
inclui os últimos dados da execução. Cada mensagem do usuário começa uma coleta
nova: o rodapé não reutiliza cotas de execuções anteriores. Já o comando
`/cotas` lê o último registro salvo (veja abaixo). Dados ausentes aparecem
como `indisponível`; janelas vencidas mostram `aguardando atualização`.

## Consulta com /cotas

`/cotas` (ou `/quota`) responde com uma única mensagem: para Claude, Codex e
Agy, nesta ordem, a cota de 5h e a semanal (percentual usado e renovação), a
idade do dado e um estado:

- **DISPONÍVEL**: nenhuma janela esgotada e nenhum limite registrado em vigor.
  No Agy, se um grupo de modelos (Gemini ou Claude/GPT) estiver esgotado e o
  outro não, ou ainda não tiver dados, a linha mostra
  `(parcial: GRUPO esgotado)`.
- **INDISPONÍVEL**: uma janela está em 100% e ainda não renovou, ou a última
  execução terminou por limite de uso. Mostra quando deve voltar. O Agy só fica
  assim quando os dois grupos são conhecidos e estão esgotados.
- **SEM DADOS**: nenhuma resposta pelo bot registrou cotas desse agente ainda.

Nenhum agente é executado só para medir a cota. Ao fim de cada execução pelo
bot, os percentuais e horários de renovação vistos são salvos em
`quota_status_state.json`, ao lado do `takopi.toml`. Claude e Agy mostram esse
último registro. O Codex é relido, a cada consulta, dos registros locais de
sessão (`~/.codex/sessions`), sem chamada de modelo, e substitui o salvo só
quando é mais recente; esses registros também podem ser antigos. Em qualquer
agente, um dado com mais de 24 horas vem com o aviso `pode estar
desatualizado`.

O limite de uso é reconhecido pelo veredito `rejected` do Claude, por
`rate_limit_reached_type` do Codex ou pelo texto de erro de limite de uso de
Claude e Codex. Uma janela recusada acima de 100% aparece como 100%, com o
próprio horário de renovação. Outras falhas, como erro de rede ou 429, não
mudam o estado. Uma resposta bem-sucedida limpa o limite e descarta janelas
esgotadas que ela não voltou a informar. Todo limite registrado tem prazo: a
renovação conhecida da janela, ou cinco horas. Janelas cujo horário de
renovação já passou aparecem como `renovada (aguardando atualização)`.
Quando falta esse horário, a validade é estimada a partir da própria janela
(5h ou sete dias) e do instante em que ela foi observada. Atualizar a outra
janela não prolonga essa validade. Arquivos anteriores, com um único horário
de observação, continuam sendo aceitos.

O arquivo guarda apenas percentuais, horários e o indicador de limite: nenhum
texto de erro, prompt, caminho, ID de sessão, plano ou crédito. Um agente sem
configuração ou sem CLI aparece como `não configurado` ou `CLI ausente`.
Os nomes `cotas` e `quota` passam a ser reservados: projetos e comandos de
plugins que usem esses nomes precisam ser renomeados antes da atualização.

## Fontes e limitações

- **Codex (`exec`)**: leitura incremental do rollout da sessão exata, com índice
  SQLite aberto somente para leitura e validação do ID. Apenas registros do
  turno atual são aceitos. `turn_context` fornece modelo/esforço;
  `token_count` fornece a última utilização do contexto, capacidade e janelas
  de 300/10080 minutos. O total acumulado de tokens não é usado como contexto.
- **Claude**: modelo vem do stream; esforço vem do evento quando disponível ou
  do `/reasoning set NÍVEL` aplicado à execução. O padrão implícito do CLI pode
  aparecer como indisponível. Contexto soma entrada, criação de cache e leitura
  de cache da última chamada principal; não soma saídas, subagentes nem todas as
  chamadas do turno. A capacidade vem de `modelUsage` para o modelo principal.
  `rate_limit_event.unifiedWindows` fornece as cotas. Eventos de limite que
  chegam depois do resultado são processados antes da entrega final.
- **Antigravity**: depois do processo principal terminar, `/model` e `/usage`
  são consultados em modo JSON, na mesma conversa e com os mesmos overrides.
  São consultas próprias do CLI, sem inferência. O timeout é de seis segundos,
  com consultas concorrentes; falhas deixam os campos indisponíveis e não
  descartam a resposta. As cotas selecionadas correspondem ao grupo Gemini ou
  Claude/GPT do modelo retornado. Famílias desconhecidas não recebem cotas de
  outro grupo. `/context` não funciona em modo print na versão verificada:
  mostramos os tokens da última chamada principal, separados do contexto
  percentual indisponível. Não usamos os contadores acumulados do resultado.

As chamadas de modelo continuam passando pelo `ai-memory run`. As consultas de
metadados do Agy não iniciam uma conversa com o modelo. Nenhum hook, statusline,
login ou padrão global dos CLIs é substituído.

O rodapé preserva o contexto do projeto e a linha canônica de retomada. O
renderizador reserva espaço para ele dentro do limite do Telegram, inclusive
em mensagens divididas e texto com emojis. Eventos de telemetria não contam
como ações/ferramentas executadas.

Referências: [Claude statusline](https://code.claude.com/docs/en/statusline),
[Antigravity headless](https://antigravity.google/docs/cli/headless),
[Antigravity cotas](https://antigravity.google/docs/cli/commands/usage).

## Validação local em 29/10/2026

Foram criadas três sessões reais no projeto isolado de testes e retomadas uma
vez, totalizando seis respostas bem-sucedidas através do ai-memory. Modelo,
esforço explicitamente definido como `low` e as duas cotas foram confirmados
nos três agentes. Codex e Claude forneceram contexto e capacidade; Agy forneceu
os tokens da última chamada e manteve a capacidade indisponível. Os IDs foram
preservados na retomada. Evidências sanitizadas em
`~/.local/share/takopi-test-project/validation/telemetry-results.json`.

Os testes automatizados cobrem isolamento entre sessões/turnos, cache e
subagentes do Claude, compactação, limites tardios, seleção de grupos do Agy,
falhas das consultas, registros incompletos, dados inválidos, cotas vencidas e
renderização no Telegram. Não houve envio automatizado de mensagens de teste
ao grupo real.

Suíte completa: **659 testes passaram**, cobertura de **82,26%** (mínimo 81%).
Ruff e formatação passaram; Ty não encontrou erros, mantendo cinco avisos nos
arquivos anteriores. A suíte emitiu um `ResourceWarning` de conexão SQLite não
fechada, sem falha de teste; sua origem não foi determinada nesta alteração.
Foram executados os comandos equivalentes ao `just check` com os binários do
ambiente virtual, pois `just`/`uv` não estavam no PATH desta sessão.

A ativação desta instalação foi preparada por um processo externo ao serviço:
ele espera a tarefa atual terminar e 30 segundos sem processos filhos antes de
reiniciar `takopi.service`. Aborta se detectar falha de entrega no Telegram ou
se a instância do serviço já tiver mudado. O resultado fica em
`~/.local/share/takopi-maintenance/telemetry-activation-result.json`; sucesso
exige nova instância e confirmação `startup.sent` no journal. Isso evita
reiniciar o bot que está executando a própria tarefa de atualização.
