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
| `GET /busca/pendencias?stage&reason&reason_class&source_id&run_id` | Pendências agrupadas e a página. |
| `POST /busca/pendencias/reprocessar` | Reabre por filtro (`stage`, `reason`, `reason_class`, `source_id`, `run_id`, `hashes`) ou `{everything:true}`. |
| `POST /busca/noticias/{hash}/reprocessar` | Reabre uma notícia. |
| `PATCH /busca/configuracao` | `{version, config}` das próximas rodadas. |
| `POST /busca/rodadas` | Rodada manual (`{day?, link_ids?}`). |

## Evidência

`GET /evidencias/corpus/{hash}` mostra no visualizador a evidência registrada no corpus
(`?original=1` baixa o arquivo).
