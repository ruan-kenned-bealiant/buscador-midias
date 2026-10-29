# Rodadas, saúde e telas: otimizações e correções de 28 e 29/10/2026

Registro das mudanças feitas entre as rodadas de 28/10 e a manhã de 29/10, com as medições que as
motivaram e o resultado. Contagens e tempos valem para as execuções citadas; o comportamento atual
está descrito nos documentos de [referência](atual/README.md). O banco manda: confira a
configuração salva antes de assumir qualquer valor desta página.

## Resumo

| Mudança | Opção / local | Padrão no código | Em uso em 29/10 |
|---|---|---|---|
| Corte pelo topo do documento e parada antecipada do download | `head_filter` | `shadow` | `enforce` |
| Auditoria por amostragem do corte | `head_audit_rate` | 0 | 0,02 |
| Leitura adaptativa dos links frios | `adaptive_polling` | `off` | `enforce` |
| Vagas extras para hosts com fila longa | `host_concurrency_max` | 1 | 3 |
| Primeira rodada do dia lê também o dia anterior | `close_previous_day` | falso | verdadeiro |
| Nova tentativa de notícias que falharam | `failed_retry_limit` | 1 | 3 |
| Saúde: duas rodadas por dia, inativação com histerese, troca de transporte, portão de frescor | `health_settings` | 24 h | 10h e 20h |
| Saúde: seleção semanal de inativos na rotina agendada | `recovery_due` | sempre | sempre |
| Ordem de leitura do documento gravada no link | `links.read_order` | `unknown` | preenchida |
| Índices por rodada e tabela de estatísticas | `ensure-indexes`, `backfill-stats` | — | aplicados |
| Tela Painel, estilo e carregamento compartilhados, tema sempre claro | `/`, `app.css`, `app.js` | — | no ar |

Os recursos novos nascem **desligados** no código; só a configuração salva os liga. Para desfazer
qualquer um, volte ao padrão: a próxima rodada já o respeita.

## Medições

### Rodadas de busca

| Rodada | Duração | Gravadas | Custo | Descoberta (itens/min) | Falha na descoberta |
|---|---|---|---|---|---|
| 28/10 11:31 | 185 min | 17.118 | US$ 62,31 | 94 | 4,7% |
| 28/10 15:28 | 113 min | 9.531 | US$ 33,52 | 112 | 5,7% |
| 29/10 01:00 | 26 min | 835 | US$ 3,49 | 480 | 0,5% |
| 29/10 06:00 | 60 min | 6.077 | US$ 23,07 | 281 | 0,6% |

As cargas diferem (a das 11:31 tinha ~28 mil notícias novas, a das 06h, ~10 mil), então a
comparação justa é a taxa por etapa: a descoberta ficou de 2,5 a 3 vezes mais rápida, atribuível à
soma do corte pelo topo, de mais workers de descoberta (24) e do catálogo limpo pela saúde (menos
links mortos esperando tempo limite). Nas duas rodadas de 29/10 todas as tarefas terminaram, sem
nada pendente; o estado `partial` significa só "terminou com alguma falha".

Etapa limitante: a **extração textual**, não a análise de contexto. Na rodada das 06h a análise
tinha espera mediana de 1 s na fila e ~36 de 64 workers ocupados; a extração tinha espera mediana
de 23 minutos. Os 20 minutos finais da rodada rodaram com 25 → 5 → 2 → 1 hosts, quase só dois
hosts grandes lidos uma página por vez. Daí `host_concurrency_max` em vez de mais workers de
análise.

Corte pelo topo (rodada das 06h, 12.488 leituras de descoberta): 41% cortadas, 23% por download
interrompido, 35% "sem mudança" (HTTP 304 ou conteúdo idêntico). A conferência por leitura
rebaixou 10 links cuja ordem contradizia o rótulo.

Leitura adaptativa: em seis rodadas seguidas, 85% dos links lidos (17.353 de 20.365) não gravaram
nenhuma notícia e 83% nem trouxeram uma URL candidata; na rodada das 06h, 74% das leituras de
descoberta foram de links assim. Hoje 8.983 dos 14.626 links ativos são frios.

Falhas: no total ~9.700 artigos estavam `failed`, 2 a 3 mil por dia, e nenhum era retentado por
rodada posterior (os 1.267 que falharam na rodada das 11:31 de 28/10 continuavam falhos no dia
seguinte). Dos erros de extração, cerca de 90% são bloqueios 401/403 (elpais.com, scmp.com,
independent.ie, wsj.com, nytimes.com…), que só o acesso pago resolve.

### Saúde das fontes (20h de 28/10)

| Rotina | Duração | Resultado |
|---|---|---|
| Links ativos (15.674) | 76 min | 13.748 saudáveis; 1.083 falha de acesso; 763 para revisão; 44 parados; 30 inválidos; 6 excluídos |
| Inativos (4.521) | 2 h 37 min | 4.327 saudáveis (quase todos ainda sem notícia recente); 156 falha de acesso; **26 reativados** |

A rotina alterou o catálogo em 1.068 inativações, 568 trocas de transporte, 6 exclusões e 26
reativações. Reativar 26 de 4.521 em 2 h 37 min motivou a seleção semanal: com `recovery_due`, a
rotina agendada testaria ~1.200 inativos por rodada em vez de ~5.500.

### Telas e consultas (tempos aquecidos, mesma máquina)

| Tela / consulta | Antes | Depois |
|---|---|---|
| `/busca` (rodada mais recente) | 29–45 s | ~2,8 s |
| Página de tarefas de uma rodada de 53 mil tarefas | 4.030 ms | 6 ms |
| Contagem total de tarefas | 190 ms | 15 ms |
| Filas por etapa | 870 ms | 420 ms |
| Aba "Por site", primeira abertura | 7,7 s | 3,8 s |
| Aba "Por site", virar página ou buscar | 7,7 s | 0 s (cache de 15 s) |
| Filas "Por site" (consulta isolada) | 24,8 s | 1,6 s |
| Filtro por site dentro de uma rodada | 4,6 s | 1,2 s |
| `/saude` | 5,7 s | 1,8–3,4 s |
| `/busca/painel` | — | 0,2–0,5 s (6 s na primeira, por causa dos links frios) |

A causa principal era a falta de índice por rodada em `ingest_jobs` (1,66 milhão de linhas) e
`ingest_occurrences` (1,09 milhão): cada contagem lia a tabela inteira. Os três índices ocupam
12 MB, 38 MB e 100 MB e foram criados em 8 a 30 s, sem bloquear a escrita.

## Por que cada decisão

- **Primeira rodada do dia lê o dia anterior.** A janela de cada rodada vai de 00h até agora. Uma
  notícia publicada às 19h só poderia ser lida por uma rodada depois das 19h, e a seguinte (01h) já
  tem a janela do dia seguinte. A primeira rodada do dia cobre essa noite sem criar uma rodada
  extra, e dá uma retentativa às falhas da última.
- **Nova tentativa só de falhas que podem passar.** Repetir um 403 de bloqueio costuma não mudar
  nada e aumenta a carga sobre o site; um tempo esgotado ou uma resposta de análise vazia
  frequentemente passam na rodada seguinte. A mesma rodada nunca repete a própria falha.
- **Link frio lido nas rodadas das pontas.** Como a janela da última rodada contém a das
  anteriores e a da primeira cobre o dia anterior, ler o link só na primeira e na última não perde
  notícia. A evidência de "frio" é por leitura **do link**: se fosse "últimas rodadas", um link
  lido só duas vezes por dia deixaria de parecer frio e voltaria a ser lido sempre.
- **Evento de inativação, não `updated_at`, para "caiu há pouco".** Uma operação em massa que
  ajusta o transporte de milhares de links atualiza `updated_at` de todos eles, e a seleção
  semanal passaria a marcar todo mundo como recente. O evento `health_inactivate` registra o
  que importa.
- **Custos fora do primeiro desenho da tela.** Somar as tentativas de uma rodada leva ~2 s com o
  cache quente e até 24 s a frio; a tela abre sem eles e os busca depois, a cada 60 s no máximo.
- **Tabela de estatísticas por rodada.** O Painel não pode somar 1,6 milhão de tarefas a cada
  abertura. Cada rodada vira uma linha, calculada uma vez ao terminar.
- **Auditoria só conta rodadas com o corte ativo.** Em modo sombra, os "cortes" são simulações de
  regras anteriores, e somá-los faria um erro já corrigido aparecer como perda de hoje.

## Pendências e riscos conhecidos

- **Bright Data.** As chaves ainda precisam ser configuradas. Os bloqueios 401/403 (4–5% das
  extrações) e a reanálise dos principais sites dependem disso.
- **Disco e vigia.** O vigia de disco usado nesses dias é um script fora do repositório e não
  sobrevive a um reinício da máquina; a política permanente (evidência em S3 ou limpeza agendada)
  continua pendente.
- **Banco compartilhado com o PowerDD.** Consultas pesadas do PowerDD já levaram o banco a
  250–480% de CPU e multiplicam qualquer tempo do buscador; meça sempre lado a lado.
- **Custo por notícia.** Entre a manhã e a noite de 26/10 os tokens de entrada por notícia gravada
  subiram de ~2.400 para ~7.000 (custo de ~US$ 0,001 para ~US$ 0,004) com a mesma configuração de
  modelo e texto. A causa não foi investigada.
- **Falhas antigas.** As ~9.700 falhas anteriores à ativação do retry só serão retentadas se
  reencontradas no feed dentro da janela; não há comando que as reprocesse em massa.
- **Catálogo.** 25 sites ainda não têm nenhum link ativo e ~1.850 links redundantes de outros
  sites não foram consolidados (`scripts/consolidate_site_links.py`).
- **Validação em produção.** O fechamento do dia (01h), a nova tentativa e a auditoria só mostram
  efeito a partir da rodada seguinte à ativação; o Painel acompanha cada uma.
