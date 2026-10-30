# Fluxo das notícias

## Rodada

A rodada é um conjunto finito de leituras. O `scheduler` cria uma por horário do calendário
(padrão 01h, 06h, 12h e 16h de Brasília; um horário só gera uma rodada, garantido por `bm_runs.slot`).
A configuração é **congelada** na rodada, mecanismos inclusos: mudar a configuração vale só para as
próximas.

- **Janela:** notícias publicadas no dia, de 00h até agora (`window_mode=today`). A primeira rodada
  do dia também lê o dia anterior quando `close_previous_day` está ligado. Há rodada manual por dia e
  por lista de links.
- **Que links entram:** os ativos de sites ativos, só as rotas de atualização (`route=updates` e
  equivalentes) quando `daily_routes_only`. Com `adaptive_polling=enforce`, links "frios" (várias
  leituras seguidas sem notícia nova) são pulados nas rodadas do meio do dia.
- **Fim:** a rodada fecha quando não há leitura nem notícia aberta que ela tenha admitido. Fecha
  `completed`, ou `partial` se alguma leitura ou notícia falhou, foi cancelada, ficou sem data ou
  sem evidência. Uma rodada nunca espera pelo trabalho de outra.

## As etapas

| Etapa | O que faz | Desfechos que encerram a notícia |
|---|---|---|
| `discovery` | Lê o feed ou o sitemap (e os filhos de um índice, em fatias) e admite as URLs candidatas. | — |
| `dedup` | Confere o corpus em lote. Nunca reanalisa o que já está completo. | `known` |
| `fetch` | Baixa a página, extrai o texto e a data, aplica os filtros de URL e de título. | `no_text`, `outside_window`, `date_unknown`, `excluded_content` |
| `context` | Análise por IA: entidades, fato adverso, categoria. | `excluded_content` (categoria rotineira sem fato adverso), `no_entities` |
| `evidence` | Captura o HTML como prova (Bright Data só para fato adverso, se habilitado). | `evidence_pending` |
| `persist` | Grava no corpus de forma idempotente (`ON CONFLICT (url_hash)`). | `persisted` |

Antes do `fetch`, o `window` descarta sem HTTP o que a data da URL ou do sitemap já exclui.
O corpo do texto é apagado do banco ao fim da notícia.

## Falhas e pendências

Cada falha grava na própria notícia a etapa (`failed_stage`), o motivo (`reason`) e a **classe**:

| Classe | Exemplos | Comportamento |
|---|---|---|
| `transient` | tempo esgotado, 408/429/5xx, resposta da IA ausente | Repetida sozinha, depois que a rodada termina, até `failed_retry_limit` passadas. |
| `blocked` | 401, 402, 403 | Um 403 já tentou os outros transportes (ver abaixo). Espera a sua decisão (ou o Bright Data). |
| `permanent` | 404, 410, fora do domínio, ciclo de redirecionamento, recusa do filtro de conteúdo do provedor | Não repete. |
| `bug` | "Falha interna" | Espera correção do código. |

A tela **Pendências** do Buscador agrupa por etapa, motivo e tipo e reabre por grupo, por site ou
uma a uma. Reabrir devolve a notícia à etapa em que falhou. A rodada que falhou não repete a
própria falha.

Cada reprocessamento manual vira um **pedido** (`bm_retry_requests`, com id) e marca as notícias
que reabriu (`bm_articles.retry_id`). No topo da tela Pendências, "Pedidos de reprocessamento" mostra
os 10 mais recentes: quantas foram reabertas, quantas ainda andam, quantas foram gravadas, quantas
terminaram de outro jeito e quantas falharam (com os motivos). O resumo atualiza sozinho a cada 5 s.
Se um pedido mais novo reabre a mesma notícia, ela passa para ele e o pedido antigo mostra quantas
saíram. Reprocessar sem encontrar nada não cria pedido.

## Filtro de conteúdo do provedor

Notícias sobre crime sexual contra menores ou violência gráfica são recusadas por alguns modelos
(`finish_reason=content_filter`, sem texto e sem tokens). A análise repete **uma vez** com o modelo
alternativo (`fallback_model`, padrão `anthropic/claude-haiku-4.5`, vazio desliga), e o modelo que
respondeu fica em `analysis.model` e em `ai_model` no corpus. O custo da etapa registra `fallbacks`.
Se o alternativo também recusa, a notícia falha na etapa `context` com o motivo `content_filtered`
(classe `permanent`): não é queda do provedor e não aciona o disjuntor. Muda-se o modelo na
configuração da busca; depois, reabrir pela tela Pendências.

## Seções que moram em outro domínio

`ingestion/flow.py` (`EXTERNAL_SECTIONS`) lista seções que o feed de um site traz, mas que
redirecionam para outro domínio (no scmp.com: `yp`, `spark`, `posties`, `young-post` e
`goldthread`). Com `url_filter=enforce` elas nem entram na fila; com `shadow` só são marcadas.

## Acesso à página da notícia

A etapa `fetch` baixa a página com o transporte do link (`access_transport`, padrão `direct`). Se o
site responde **403**, ela tenta `curl_chrome_h2`, `curl_chrome_h1` e `curl_firefox_h2`, um de cada
vez (cada leitor fecha antes do próximo abrir, porque segura a vaga de acesso ao host), e o primeiro
que abre vale para as próximas notícias do mesmo site (memória do processo, até 512 sites). Só o 403
dispara a troca: 401 é paywall, 429 e 5xx têm repetição própria e 404 é página inexistente. O custo da
etapa registra `transport_fallbacks` (a troca resolveu) e `transport_extra_requests` (pedidos a mais).
O que continua sem sair: login (Mediahuis), redirecionamento para outro domínio (o leitor recusa de
propósito) e proteções que nenhum transporte atravessa.

## Retenção

A linha de `bm_articles` é a memória de que a URL já foi vista, e não precisa durar para sempre. Uma rotina a cada 10 minutos (`conferencia-corpus`) encerra as notícias em aberto cujo artigo o corpus já
tem, para não gastar IA com elas. As
gravadas e conhecidas saem depois de 3 dias e as descartadas depois de 14 (o texto delas, depois de
1); falhas e trabalho em andamento ficam. Os artigos continuam consultáveis na tela **Artigos**, que
lê o corpus. Detalhes e
horários em [OPERACAO](OPERACAO.md#limpeza-periódica).

## Fila e concorrência

- A notícia é reservada por compare-and-set (`state`, `stage`, `owner`, `attempts`); mensagem
  duplicada ou atrasada é descartada.
- **Justiça por host:** o publisher alterna entre hosts e dá vagas extras a um host só quando há
  fila longa; `bm_host_state` guarda a espera depois de um `Retry-After`.
- **Contrapressão:** uma etapa para de publicar quando a seguinte acumulou notícias demais e quando
  o disco está abaixo da reserva.
- **Lease:** reservas vencidas voltam para `retry` (ou falham, se esgotaram as tentativas).
