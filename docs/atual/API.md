# API local

Base: `/api/gerenciador`. Respostas em JSON; erros trazem `{"error": ...}`. Escritas (`POST`, `PATCH`,
`DELETE`) só são aceitas do próprio computador e exigem o cabeçalho `X-Catalog-Token` (o token vem
de `GET /resumo`). Datas em ISO 8601.

## Cadastro

| Método e caminho | Função |
|---|---|
| `GET /resumo` | Contagens do cadastro, `csrf_token`, se há credencial Bright Data. |
| `GET /fontes`, `POST /fontes`, `GET/PATCH/DELETE /fontes/{id}` | Sites (revisão otimista por `version`). |
| `GET/POST /fontes/{id}/mecanismos`, `PATCH/DELETE /mecanismos/{id}` | Links do site. |
| `GET /modelos`, `GET/PATCH /modelos/{modelo}` | Parâmetros de cada mecanismo. |
| `POST /testar`, `GET /testes/{id}` | Teste interativo de um link (até dois ao mesmo tempo). |

## Saúde

`GET /saude`, `GET /saude/rodadas/{id}?offset&category`, `POST /saude/rodadas` (`{kind, ...opções}`),
`POST /saude/rodadas/{id}/cancelar`, `PATCH /saude/configuracoes/{active|recovery}`.

## Busca

| Método e caminho | Função |
|---|---|
| `GET /busca?run_id&stage&state&source_id&offset` | Filas por etapa, rodadas, funil, página de notícias ou leituras, `broker`. `run_id=latest` resolve a mais recente. |
| `GET /busca/painel?days=1..30` | Tudo da tela Painel. |
| `GET /busca/fontes?run_id&search&offset` | Progresso por site. |
| `GET /busca/fontes/{id}/corpus?run_id` | Notícias do site já no corpus. |
| `GET /busca/noticias/{hash}` | A notícia, com a origem. |
| `GET /busca/custos?run_id&source_id` | Chamadas pagas, tokens e custo por etapa. |
| `GET /busca/pendencias?stage&reason&reason_class&source_id&run_id` | Pendências agrupadas, a página e `retries`: os 10 pedidos de reprocessamento mais recentes com o andamento (`requested`, `in_progress`, `persisted`, `done_other`, `failed`, `cancelled`, `superseded`, `finished`, `reasons`). |
| `POST /busca/pendencias/reprocessar` | Reabre por filtro (`stage`, `reason`, `reason_class`, `source_id`, `run_id`, `hashes`) ou `{everything:true}`. Responde `{requeued, retry_id}`; `retry_id` é nulo se nada foi reaberto. |
| `POST /busca/noticias/{hash}/reprocessar` | Reabre uma notícia (também gera `retry_id`). |
| `GET /busca/artigos?q&field&site_id&alert&since&until&scope&offset&limit` | Artigos do corpus por data de entrada, cada um com as entidades e, em cada entidade, o contexto e as análises (alerta, confiança, justificativa, fato adverso). `field`: `all` (entidade, URL, título), `entity`, `url`, `title`, `alert` (texto da análise, mais lento). `scope`: `searcher` (o que o buscador trouxe) ou `all`. A contagem para em 10.000 (`capped`). |
| `GET /busca/limpeza` | As categorias de limpeza periódica: horário, idade, quantas linhas esperam, últimas execuções e a próxima. |
| `PATCH /busca/configuracao` | `{version, config}` das próximas rodadas. |
| `POST /busca/rodadas` | Rodada manual (`{day?, link_ids?}`). |

## Evidência

`GET /evidencias/corpus/{hash}` mostra no visualizador a evidência registrada no corpus (o `{hash}` é o `url_hash` do artigo; a busca é direta no corpus, então o link continua valendo depois que a limpeza apaga a linha de `bm_articles`)
(`?original=1` baixa o arquivo).
