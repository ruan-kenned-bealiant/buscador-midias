# Dados

Tudo vive no schema `public`. As tabelas do buscador levam o prefixo `bm_`; os sites são a exceção e
ficam na tabela de fontes confiáveis do corpus, `midias_ia_trusted_sources`, que o buscador mantém.
O DDL está em `db/schema.py` e é idempotente (`python -m buscador_midias db install`).

## Cadastro (preservado entre rodadas)

| Tabela | Conteúdo |
|---|---|
| `midias_ia_trusted_sources` | Os sites. É a tabela de fontes confiáveis do corpus (nome, país, `country_code`, idioma, região, tipo, `url_without_https_www`…) com as colunas do buscador: `identity` (única entre os sites que a busca adotou; linhas repetidas da lista original ficam sem identidade), `active` (agora quer dizer "o buscador lê este site"), `deleted_at`, `deactivated_by/reason/at` e `version`. Só entram na busca as linhas com `identity`. |
| `bm_links` | Feed ou sitemap de um site: URL, `kind`, `mechanism`, `access_transport`, `read_order`, `route` (`updates`, `archive`, `review`), `search_interval_seconds`, exclusão lógica e autoria da inativação (`user`, `health`, `validation`, `system`), `version`. `UNIQUE(site_id,url)` inclusive excluídos. |
| `bm_link_state` | O que a máquina escreve sobre o link, sem `version`: `last_test`, `evidence`, `health_category`, `health_failures`, `health_checked_at`, `health_config` (o que a saúde testou), `last_read_at`. |
| `bm_mechanisms` | Parâmetros de cada mecanismo e sua `version`. |
| `bm_settings` | Um escopo por linha: `search`, `health_active`, `health_recovery`. `value` JSON, `version`, `next_run_at`. |
| `bm_events` | Auditoria: `entity`, `action`, `actor`, `changes` (só o que mudou). |

## Rodadas

| Tabela | Conteúdo |
|---|---|
| `bm_runs` | `type` (`search` ou `health`), `kind`, `trigger`, `status`, **`config` congelada** (inclui os parâmetros dos mecanismos), `since/until`, `slot` único (relógio), `stats` e `stats_final`. Uma rodada de saúde aberta por `kind`. |
| `bm_run_links` | Um link na rodada: versões congeladas; na busca, `listed/new/known` e `discovery_done`; na saúde, `state`, `category`, `attempts`, `result` (com `attempt_log`) e `activated`. |
| `bm_discovery_tasks` | Um documento XML por linha (raiz e filhos de sitemap) com colunas de fila e o plano compactado das continuações. |

## A notícia

`bm_articles` é a única tabela da notícia. Chave: `hash` (hash canônico da URL, igual ao do PHP).

- **Origem:** `site_id`, `link_id`, `document_url`, `first_run_id`, `run_id` (a rodada que a admitiu por último), `first_seen_at`, `last_seen_at`, `seen_count`.
- **Fila:** `stage`, `state`, `outcome`, `attempts`, `passes`, `lane`, `next_attempt_at`, `locked_until`, `queued_at`, `owner`.
- **Falha (a "pendência"):** `failed_stage`, `reason`, `reason_class`, `retryable`, `failed_at`, `detail`.
- **Reprocessamento manual:** `retry_id` aponta para `bm_retry_requests` (`id`, `created_at`, `filters`, `requested`): um registro por pedido feito na tela Pendências, com o filtro usado (sem a lista de hashes). Serve só ao resumo da tela; apagar um pedido apenas solta o vínculo.
- **Dados:** `payload`, `cost` (por etapa), `history` (últimas tentativas), `corpus_article_id`.

Estados: `pending`, `queued`, `running`, `retry`, `done`, `failed`, `cancelled`.
Desfechos (`outcome`): `persisted`, `known`, `outside_window`, `date_unknown`, `no_text`,
`excluded_content`, `no_entities`, `evidence_pending`.

## Apoio

`bm_host_state` (espera por host depois de um `Retry-After`) e `bm_document_cache` (GET condicional).

## O corpus

O corpus (`midias_ia_articles`, `midias_ia_article_entities`…) pertence ao KyX/PowerDD e só recebe
linhas pela etapa `persist`. A única tabela dele que o buscador altera é `midias_ia_trusted_sources`,
da qual passou a ser o dono: os leitores do KyX e do PowerDD que filtram por `active` passarão a
enxergar o significado novo e serão ajustados do lado deles. O comando de reimportação da planilha
do KyX apaga a tabela inteira (`delete()` e insere de novo) e **não deve mais ser usado**: ele
perderia as colunas do buscador e falharia pelas chaves estrangeiras de `bm_links`, `bm_articles`,
`bm_run_links` e `bm_document_cache`.

`bm_articles` não é o corpus: só ~13% das linhas viram artigo. Ao concluir como `persisted` ou
`known`, a linha perde corpo, análise e evidência (já estão no corpus) e guarda o vínculo
`corpus_article_id`.
