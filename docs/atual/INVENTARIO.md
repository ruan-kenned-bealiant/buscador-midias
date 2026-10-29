# Inventário do repositório

Índice revisado em 26/10/2026. Os resumos abaixo identificam a finalidade declarada dos arquivos; os contratos operacionais e suas ressalvas estão nos demais documentos desta seção. Arquivos `__init__.py` organizam os pacotes. Cache, ambientes virtuais, segredos e artefatos volumosos de `var/` não são código da aplicação.

## Pacotes e módulos

### `analysis`

| Arquivo | Responsabilidade |
|---|---|
| [classification.py](../../src/buscador_midias/analysis/classification.py) | Vocabulário fechado de conteúdo, independente do risco e do estado da fila. |
| [context.py](../../src/buscador_midias/analysis/context.py) | Análise de contexto: contrato, leitura defensiva da resposta e adaptador do OpenRouter. |
| [headline.py](../../src/buscador_midias/analysis/headline.py) | Texto de manchete: título do sitemap ou slug da URL, e as palavras que o compõem. |
| [mechanism_metrics.py](../../src/buscador_midias/analysis/mechanism_metrics.py) | Contribuição observada e simulação retrospectiva, sem selecionar plano de coleta. |
| [mechanism_selection.py](../../src/buscador_midias/analysis/mechanism_selection.py) | Classificação rastreável de mecanismos, sem executar ou modificar a coleta. |
| [openrouter.py](../../src/buscador_midias/analysis/openrouter.py) | Transporte do OpenRouter (API compatível com chat completions) usado pela análise de contexto. |
| [prompt.py](../../src/buscador_midias/analysis/prompt.py) | Prompt da análise de contexto de uma matéria (independente de entidade). |
| [replies.py](../../src/buscador_midias/analysis/replies.py) | Leitura da resposta em JSON de um modelo de linguagem. |
| [themes.py](../../src/buscador_midias/analysis/themes.py) | Temas que a análise de contexto reconhece (slugs de `midias_ia_theme_prompts`). |

### `catalog`

| Arquivo | Responsabilidade |
|---|---|
| [batch.py](../../src/buscador_midias/catalog/batch.py) | Rodada finita, retomável e auditada para conservar apenas links comprovadamente úteis. |
| [batch_reader.py](../../src/buscador_midias/catalog/batch_reader.py) | Rede compartilhada da rodada: cache limitado em memória e intervalo global por host. |
| [bright_reader.py](../../src/buscador_midias/catalog/bright_reader.py) | Fallback pago integrado ao leitor do gerenciador, com os mesmos controles de acesso. |
| [cadence.py](../../src/buscador_midias/catalog/cadence.py) | Estimativa de publicação por amostra; lastmod de sitemap não é data de publicação. |
| [compression.py](../../src/buscador_midias/catalog/compression.py) | Descompressão limitada compartilhada entre transporte HTTP e mecanismos XML. |
| [consolidation.py](../../src/buscador_midias/catalog/consolidation.py) | Consolidação explícita das versões estaduais do g1, preservando a origem. |
| [curl_reader.py](../../src/buscador_midias/catalog/curl_reader.py) | Transporte curl opcional, com os mesmos limites e proteções do leitor direto. |
| [editorial_mapping.py](../../src/buscador_midias/catalog/editorial_mapping.py) | Critérios puros para aplicar provas de mapeamento sem confundir arquivo com atualização. |
| [health_activation.py](../../src/buscador_midias/catalog/health_activation.py) | Ativação explícita de recuperados, com prova textual e trilha de auditoria. |
| [health_policy.py](../../src/buscador_midias/catalog/health_policy.py) | Classifica o teste real sem confundir falta de notícias com falha de acesso. |
| [health_restoration.py](../../src/buscador_midias/catalog/health_restoration.py) | Proveniência e revisões exigidas para recuperar exclusões automáticas. |
| [health_schema.py](../../src/buscador_midias/catalog/health_schema.py) | Migração aditiva da verificação periódica de saúde no PostgreSQL. |
| [health_store.py](../../src/buscador_midias/catalog/health_store.py) | Fila finita e histórico persistente das duas rotinas de saúde das fontes. |
| [health_worker.py](../../src/buscador_midias/catalog/health_worker.py) | Agendador retomável: uma liderança PostgreSQL e filas finitas por rodada. |
| [http_policy.py](../../src/buscador_midias/catalog/http_policy.py) | Classificação de falhas HTTP e espera informada pelo servidor. |
| [http_session.py](../../src/buscador_midias/catalog/http_session.py) | Sessões HTTP públicas e conexões ociosas limitadas, isoladas por fonte e processo. |
| [http_transport.py](../../src/buscador_midias/catalog/http_transport.py) | Perfis explícitos de transporte, independentes do mecanismo de parsing e seleção. |
| [mechanisms/automatic.py](../../src/buscador_midias/catalog/mechanisms/automatic.py) | Mecanismo automático: reconhece formatos e percorre índices quando presentes. |
| [mechanisms/base.py](../../src/buscador_midias/catalog/mechanisms/base.py) | Contrato dos mecanismos e parâmetros validados de configuração. |
| [mechanisms/g1_daily.py](../../src/buscador_midias/catalog/mechanisms/g1_daily.py) | Plano explícito de atualizações do g1, usando somente filhos do índice oficial. |
| [mechanisms/news_sitemap.py](../../src/buscador_midias/catalog/mechanisms/news_sitemap.py) | Mecanismo de sitemap com metadados Google News, sem restrição de publicação. |
| [mechanisms/parsing.py](../../src/buscador_midias/catalog/mechanisms/parsing.py) | Interpretação XML compartilhada; regras de aceitação ficam em cada mecanismo. |
| [mechanisms/rss.py](../../src/buscador_midias/catalog/mechanisms/rss.py) | Mecanismo de RSS 2.0, Atom e RSS 1.0/RDF. |
| [mechanisms/sitemap.py](../../src/buscador_midias/catalog/mechanisms/sitemap.py) | Mecanismo de sitemap direto, inclusive arquivos gzip e zip. |
| [mechanisms/sitemap_index.py](../../src/buscador_midias/catalog/mechanisms/sitemap_index.py) | Mecanismo de navegação por índices aninhados de sitemaps. |
| [migrate_postgres.py](../../src/buscador_midias/catalog/migrate_postgres.py) | Importação atômica e conferida do catálogo SQLite para o PostgreSQL de Mídias. |
| [models.py](../../src/buscador_midias/catalog/models.py) | Metadados dos mecanismos, mantidos como fachada para os consumidores existentes. |
| [postgres.py](../../src/buscador_midias/catalog/postgres.py) | Repositório PostgreSQL: mantém o contrato do gerenciador e usa transações reais. |
| [postgres_schema.py](../../src/buscador_midias/catalog/postgres_schema.py) | DDL PostgreSQL do gerenciador, portável para futuras migrations do powerDD. |
| [reader.py](../../src/buscador_midias/catalog/reader.py) | Leitor HTTP finito, sem proxy nem consulta a robots, com conexão em IP público. |
| [review.py](../../src/buscador_midias/catalog/review.py) | Auditoria finita do catálogo local; sem downloads nem descoberta de novos caminhos. |
| [schema.py](../../src/buscador_midias/catalog/schema.py) | Modelo relacional local e migração transacional do cadastro destinado à base de Mídias. |
| [storage.py](../../src/buscador_midias/catalog/storage.py) | Escolha explícita do repositório; após corte, falha PostgreSQL nunca volta ao SQLite. |
| [store.py](../../src/buscador_midias/catalog/store.py) | Catálogo persistente do MVP, com importação única e exclusão lógica auditada. |
| [store_errors.py](../../src/buscador_midias/catalog/store_errors.py) | Erros seguros de domínio compartilhados entre cadastro e testes de leitura. |
| [testing.py](../../src/buscador_midias/catalog/testing.py) | Testes interativos em amostra, com progresso e resultado independente de recência. |
| [freshness.py](../../src/buscador_midias/catalog/freshness.py) | Frescor de um link: há quanto tempo o próprio feed ou sitemap deixou de trazer notícia nova. |
| [head_probe.py](../../src/buscador_midias/catalog/head_probe.py) | Decide, só pelo começo de um documento, se ele inteiro está fora da janela. |
| [health_actions.py](../../src/buscador_midias/catalog/health_actions.py) | Inativação e exclusão automáticas da rotina de saúde, com trilha de auditoria. |
| [health_rules.py](../../src/buscador_midias/catalog/health_rules.py) | Regras da rotina de saúde: decisão sobre cada link e agenda por horários fixos. |
| [link_period.py](../../src/buscador_midias/catalog/link_period.py) | Período que o endereço de um link cobre: fatia de dia ou de mês que já fechou é histórica. |
| [link_review.py](../../src/buscador_midias/catalog/link_review.py) | Sonda de um link do cadastro: lê o documento ao vivo e propõe o que fazer, sem alterar nada. |
| [link_theme.py](../../src/buscador_midias/catalog/link_theme.py) | Tema inútil de um link (clima, horóscopo, receita, cinema…), pelo caminho e pelo rótulo. |
| [mapping_apply.py](../../src/buscador_midias/catalog/mapping_apply.py) | Aplicação das propostas de mapeamento ao cadastro, depois de revalidar cada candidato. |
| [read_order.py](../../src/buscador_midias/catalog/read_order.py) | Ordem em que o documento de um link lista suas entradas, medida uma vez e gravada no link. |
| [read_order_backfill.py](../../src/buscador_midias/catalog/read_order_backfill.py) | Preenche `links.read_order` a partir dos documentos já interpretados, sem tocar na rede. |
| [redundancy.py](../../src/buscador_midias/catalog/redundancy.py) | Links de um mesmo site que não trazem nada além do que os outros já trazem. |
| [review_cli.py](../../src/buscador_midias/catalog/review_cli.py) | Revisão dos links do cadastro em lotes de sites: `probe` sonda e relata, `apply` executa. |
| [site_budget.py](../../src/buscador_midias/catalog/site_budget.py) | Teto de chamadas pagas por site, somado entre todas as leituras daquele site. |

### `corpus`

| Arquivo | Responsabilidade |
|---|---|
| [article.py](../../src/buscador_midias/corpus/article.py) | Do achado da análise ao artigo do corpus de Mídias IA, no formato que o KyX já usa. |
| [country.py](../../src/buscador_midias/corpus/country.py) | Origem editorial: região do idioma, país do artigo e cadastro da fonte, nesta ordem. |
| [index.py](../../src/buscador_midias/corpus/index.py) | Índice somente leitura dos `url_hash` já presentes no corpus de Mídias IA (Postgres). |
| [sync.py](../../src/buscador_midias/corpus/sync.py) | Sincronização dos achados com o corpus: grava o que é novo e acompanha a evidência. |
| [writer.py](../../src/buscador_midias/corpus/writer.py) | Gravação dos achados no corpus de Mídias IA (Postgres), direto, de forma idempotente. |

### `dashboard`

| Arquivo | Responsabilidade |
|---|---|
| [access_audit.py](../../src/buscador_midias/dashboard/access_audit.py) | Retrato local das auditorias de acesso, sem rede ou gravações no banco. |
| [catalog_api.py](../../src/buscador_midias/dashboard/catalog_api.py) | API local de escrita do gerenciador, isolada das consultas históricas. |
| [functional.py](../../src/buscador_midias/dashboard/functional.py) | Consulta de RSS/sitemaps funcionais e contagens com escopo explícito. |
| [guide.py](../../src/buscador_midias/dashboard/guide.py) | Guia de interpretação com mecanismos e parâmetros obtidos do registro real. |
| [mapping.py](../../src/buscador_midias/dashboard/mapping.py) | Consulta do mapa estrutural e de amostras observadas, sem chamadas aos sites. |
| [round_view.py](../../src/buscador_midias/dashboard/round_view.py) | Consulta isolada da rodada 12, sem custos históricos ou etapas de enriquecimento. |
| [schedule.py](../../src/buscador_midias/dashboard/schedule.py) | Agenda das execuções, para o painel mostrar o que já rodou e o que vem a seguir. |
| [selection.py](../../src/buscador_midias/dashboard/selection.py) | Leitura do pacote estático de seleção, sem conexão ao corpus ou acesso aos veículos. |
| [server.py](../../src/buscador_midias/dashboard/server.py) | Servidor local do gerenciador e das consultas históricas do buscador. |
| [snapshot.py](../../src/buscador_midias/dashboard/snapshot.py) | Retrato do estado do fluxo, em um dicionário serializável em JSON, para alimentar o painel. |

### `discovery`

| Arquivo | Responsabilidade |
|---|---|
| [document_queue.py](../../src/buscador_midias/discovery/document_queue.py) | Fila durável por documento, com alternância de fontes e exclusão de hosts ocupados. |
| [explore.py](../../src/buscador_midias/discovery/explore.py) | Caminhamento por robots.txt e sitemaps de um site, compartilhado por sondagem e descoberta. |
| [feed_explore.py](../../src/buscador_midias/discovery/feed_explore.py) | Exploração do feed (RSS/Atom) de um site: a alternativa ao sitemap quando ele não serve. |
| [feeds.py](../../src/buscador_midias/discovery/feeds.py) | Descoberta por feed (RSS/Atom) e pela API REST do WordPress, como alternativa ao sitemap. |
| [incremental.py](../../src/buscador_midias/discovery/incremental.py) | Descoberta incremental: a cada rodada, só o que ainda não vimos e que de fato é notícia nova. |
| [probe.py](../../src/buscador_midias/discovery/probe.py) | Sondagem de viabilidade da descoberta: o que cada site oferece via robots.txt e sitemaps. |
| [report.py](../../src/buscador_midias/discovery/report.py) | Execução em lote da sondagem e geração do relatório de viabilidade. |
| [robots.py](../../src/buscador_midias/discovery/robots.py) | Interpretação do robots.txt: regras de acesso e sitemaps declarados. |
| [sitemap_parser.py](../../src/buscador_midias/discovery/sitemap_parser.py) | Leitura defensiva de sitemaps (XML, `.xml.gz` e `.zip`) vindos de sites de terceiros. |
| [sources.py](../../src/buscador_midias/discovery/sources.py) | Fontes de descoberta de um site e a cascata que as tenta em ordem. |
| [stats.py](../../src/buscador_midias/discovery/stats.py) | Estatísticas das rodadas de descoberta: o que cada rodada rendeu e onde há sinais de problema. |
| [strategy_set.py](../../src/buscador_midias/discovery/strategy_set.py) | Leitura do principal/reserva e dos complementos, com deduplicação canônica. |
| [url_filter.py](../../src/buscador_midias/discovery/url_filter.py) | Filtro pela forma da URL (L0): decide sem baixar nada. |
| [xml_review.py](../../src/buscador_midias/discovery/xml_review.py) | Leitura de um único mecanismo para revisão finita, sem expansão ou filtro de data. |

### `domain`

| Arquivo | Responsabilidade |
|---|---|
| [dated_url.py](../../src/buscador_midias/domain/dated_url.py) | URLs com data embutida: sitemaps publicados um por dia ou por mês. |
| [discovered.py](../../src/buscador_midias/domain/discovered.py) | Modelo de uma URL descoberta em um sitemap ou feed. |
| [feed.py](../../src/buscador_midias/domain/feed.py) | Modelo de um item de feed. |
| [mapping.py](../../src/buscador_midias/domain/mapping.py) | Inventário de páginas: exploração da estrutura, independente da coleta de notícias. |
| [roles.py](../../src/buscador_midias/domain/roles.py) | Papéis de uma entidade em uma matéria e quais deles tornam a notícia adversa. |
| [site.py](../../src/buscador_midias/domain/site.py) | Modelo de domínio de uma fonte de notícias confiável. |
| [sitemap.py](../../src/buscador_midias/domain/sitemap.py) | Modelos de um sitemap já interpretado. |
| [strategy.py](../../src/buscador_midias/domain/strategy.py) | Modelos da estratégia de descoberta de uma fonte: o que foi sondado e o que foi escolhido. |
| [url.py](../../src/buscador_midias/domain/url.py) | Canonicalização de URL e `url_hash`, em paridade com o `MidiasIaUrlCanonicalizer` do KyX. |

### `enrich`

| Arquivo | Responsabilidade |
|---|---|
| [pipeline.py](../../src/buscador_midias/enrich/pipeline.py) | Enriquecimento das URLs novas: texto, análise de contexto, critério e evidência. |

### `evidence`

| Arquivo | Responsabilidade |
|---|---|
| [capture.py](../../src/buscador_midias/evidence/capture.py) | Captura de evidência: reaproveita o HTML do download direto só se for confiável. |
| [s3_storage.py](../../src/buscador_midias/evidence/s3_storage.py) | Armazenamento da evidência em S3 (ou compatível, como o MinIO) e escolha do armazenamento. |
| [storage.py](../../src/buscador_midias/evidence/storage.py) | Armazenamento da evidência: porta e implementação em disco local. |

### `extraction`

| Arquivo | Responsabilidade |
|---|---|
| [compare.py](../../src/buscador_midias/extraction/compare.py) | Comparação entre duas cópias da mesma página (ex.: download direto x Bright Data). |
| [fetch.py](../../src/buscador_midias/extraction/fetch.py) | Download direto de artigos: educado (robots.txt, intervalo por host) e sem fallback pago. |
| [html.py](../../src/buscador_midias/extraction/html.py) | Extração do conteúdo de uma página de notícia: título, texto, data de publicação e idioma. |
| [reliability.py](../../src/buscador_midias/extraction/reliability.py) | Avaliação de confiabilidade de uma página como evidência. |
| [sample_audit.py](../../src/buscador_midias/extraction/sample_audit.py) | Verificação de amostras de notícias sem IA ou seleção automática de mecanismos. |

### `ingestion`

| Arquivo | Responsabilidade |
|---|---|
| [admission.py](../../src/buscador_midias/ingestion/admission.py) | Seleção justa por hostname antes de limitar publicações de acesso HTTP. |
| [api.py](../../src/buscador_midias/ingestion/api.py) | API local do buscador, sob o mesmo token e proteção de origem do gerenciador. |
| [artifacts.py](../../src/buscador_midias/ingestion/artifacts.py) | HTML de trabalho comprimido, fora das mensagens e separado da evidência definitiva. |
| [broker.py](../../src/buscador_midias/ingestion/broker.py) | Transporte RabbitMQ durável; o PostgreSQL é a fonte dos checkpoints. |
| [content_filter.py](../../src/buscador_midias/ingestion/content_filter.py) | Conteúdo rotineiro sem interesse (horóscopo, clima, loteria, receita, logística de jogo). |
| [daily_migration.py](../../src/buscador_midias/ingestion/daily_migration.py) | Separa operações antigas sem apagar linhas, filas ou documentos do corpus. |
| [engine.py](../../src/buscador_midias/ingestion/engine.py) | Executores independentes: RSS/XML, corpus, texto, contexto, evidência e persistência. |
| [evidence_view.py](../../src/buscador_midias/ingestion/evidence_view.py) | Leitura da evidência registrada no corpus, sem executar seu HTML no painel. |
| [flow.py](../../src/buscador_midias/ingestion/flow.py) | Limites de trabalho em trânsito, independentes do número total de links. |
| [g1_pilot.py](../../src/buscador_midias/ingestion/g1_pilot.py) | Piloto finito do plano diário do g1, isolado do backlog e das filas de produção. |
| [progress.py](../../src/buscador_midias/ingestion/progress.py) | Progresso por fonte e rodada, sem contar uma notícia duas vezes pelos seus links. |
| [routes.py](../../src/buscador_midias/ingestion/routes.py) | Rotas temporais calibradas pelas provas textuais já existentes no catálogo. |
| [runtime.py](../../src/buscador_midias/ingestion/runtime.py) | Serviço supervisionável: agendador, outbox e workers de seis filas RabbitMQ. |
| [schema.py](../../src/buscador_midias/ingestion/schema.py) | DDL aditivo da ingestão, separado do corpus e das rodadas históricas. |
| [sources_pilot.py](../../src/buscador_midias/ingestion/sources_pilot.py) | Teste finito de links conhecidos, separado das filas e do catálogo em produção. |
| [store.py](../../src/buscador_midias/ingestion/store.py) | Agenda e checkpoints transacionais: mensagens transportam somente identificadores. |
| [cold_links.py](../../src/buscador_midias/ingestion/cold_links.py) | Leitura adaptativa: links que não rendem há várias leituras só são lidos nas rodadas das pontas. |
| [panel.py](../../src/buscador_midias/ingestion/panel.py) | Dados da tela Painel: última rodada, histórico, estado atual, falhas, saúde e auditoria. |
| [run_stats.py](../../src/buscador_midias/ingestion/run_stats.py) | Estatísticas pré-agregadas por rodada, para o painel ler números prontos. |

### `labeling`

| Arquivo | Responsabilidade |
|---|---|
| [csvio.py](../../src/buscador_midias/labeling/csvio.py) | Leitura tolerante dos CSVs de rotulagem (a pessoa pode ter salvado pelo Excel). |
| [export.py](../../src/buscador_midias/labeling/export.py) | Exportação de lotes de rotulagem às cegas. |
| [score.py](../../src/buscador_midias/labeling/score.py) | Pontuação dos rótulos humanos: precisão do corpus e recall estimado da análise de contexto. |
| [server.py](../../src/buscador_midias/labeling/server.py) | Servidor local da tela de rotulagem: uma página, a lista cega e um endpoint de voto. |
| [session.py](../../src/buscador_midias/labeling/session.py) | Sessão de rotulagem: lê os lotes cegos e grava o veredito de cada item direto no CSV. |
| [verdicts.py](../../src/buscador_midias/labeling/verdicts.py) | Vocabulário dos vereditos humanos, compartilhado pela pontuação e pela tela de rotulagem. |

### `net`

| Arquivo | Responsabilidade |
|---|---|
| [brightdata.py](../../src/buscador_midias/net/brightdata.py) | Cliente do Bright Data Web Unlocker, usado como fallback para sites que bloqueiam o crawler. |
| [budget.py](../../src/buscador_midias/net/budget.py) | Orçamento por fonte, compartilhado entre os mecanismos de descoberta. |
| [fallback.py](../../src/buscador_midias/net/fallback.py) | Política de fallback: quando e como recorrer a um fetcher pago para sites que bloqueiam. |
| [fetcher.py](../../src/buscador_midias/net/fetcher.py) | Fetchers HTTP: cliente com limite de tamanho e decorador de intervalo por host. |
| [limited.py](../../src/buscador_midias/net/limited.py) | Teto de chamadas para um fetcher pago (controle de custo). |
| [memoizing.py](../../src/buscador_midias/net/memoizing.py) | Decorador que lembra as respostas dos hosts que várias fontes compartilham. |
| [stack.py](../../src/buscador_midias/net/stack.py) | Montagem da pilha de fetchers: HTTP direto, retry, fallback opcional e intervalo por host. |

### `sites`

| Arquivo | Responsabilidade |
|---|---|
| [loader.py](../../src/buscador_midias/sites/loader.py) | Carga e deduplicação do catálogo de fontes a partir do CSV versionado. |
| [seed_import.py](../../src/buscador_midias/sites/seed_import.py) | Extração das fontes confiáveis a partir do seed SQL do powerdd-backend. |

### `state`

| Arquivo | Responsabilidade |
|---|---|
| [migrate.py](../../src/buscador_midias/state/migrate.py) | Migração do estado antigo em SQLite (`var/state.db`) para as tabelas `midias_crawler_*`. |
| [store.py](../../src/buscador_midias/state/store.py) | Estado persistente em Postgres: o que já vimos, o que mudou e como cada rodada se saiu. |

### `strategy`

| Arquivo | Responsabilidade |
|---|---|
| [discovery_plan.py](../../src/buscador_midias/strategy/discovery_plan.py) | Leitura de um plano explícito de mecanismos validados, sem alterar estratégias no banco. |
| [evaluate.py](../../src/buscador_midias/strategy/evaluate.py) | Avalia um endereço candidato: baixa, lê (sitemap, índice ou feed), mede e julga. |
| [export.py](../../src/buscador_midias/strategy/export.py) | Planilha de conferência das estratégias: uma linha por fonte, mais candidatos e seções. |
| [health.py](../../src/buscador_midias/strategy/health.py) | Quando reavaliar a estratégia de uma fonte (regras explícitas sobre o que as rodadas mostram). |
| [hints.py](../../src/buscador_midias/strategy/hints.py) | Dicas de endereços por fonte, vindas de um levantamento em CSV. |
| [listing.py](../../src/buscador_midias/strategy/listing.py) | Varredura de listagens: da home às páginas de seção, atrás de links de matéria. |
| [manual.py](../../src/buscador_midias/strategy/manual.py) | Regras manuais por fonte: excluir um endereço (ou um prefixo) ou forçar outro como principal. |
| [mapping.py](../../src/buscador_midias/strategy/mapping.py) | Mapeamento retomável da estrutura do site, com checkpoint por página. |
| [mapping_budget.py](../../src/buscador_midias/strategy/mapping_budget.py) | Orçamento total da fonte, compartilhado entre páginas, robots e mecanismos. |
| [mapping_coverage.py](../../src/buscador_midias/strategy/mapping_coverage.py) | Leitura dos mecanismos e comparação de suas URLs com amostras das seções. |
| [mapping_export.py](../../src/buscador_midias/strategy/mapping_export.py) | Relatório do inventário completo, sem confundir exploração com cobertura. |
| [mapping_net.py](../../src/buscador_midias/strategy/mapping_net.py) | HTTP gratuito com orçamento de leituras e robots por origem para o mapeamento. |
| [mapping_priorities.py](../../src/buscador_midias/strategy/mapping_priorities.py) | Prioridades de complementação calculadas dos checkpoints, sem novas buscas. |
| [mapping_review.py](../../src/buscador_midias/strategy/mapping_review.py) | Triagem das entradas do inventário, sem alterar escopo nem políticas de acesso. |
| [measure.py](../../src/buscador_midias/strategy/measure.py) | Mede o que um sitemap ou feed publica agora (parte pura, sem rede). |
| [mechanism_inventory.py](../../src/buscador_midias/strategy/mechanism_inventory.py) | Inventário reutilizável de RSS/sitemaps e associação por evidência armazenada. |
| [page_classification.py](../../src/buscador_midias/strategy/page_classification.py) | Evidência conservadora do tipo de página, sem depender só do formato da URL. |
| [probe.py](../../src/buscador_midias/strategy/probe.py) | Sondagem de uma fonte: gera candidatos em etapas, avalia, pontua e escolhe. |
| [scoring.py](../../src/buscador_midias/strategy/scoring.py) | Veredito, nota e escolha das estratégias de uma fonte (regras explícitas, sem aprendizado). |
| [section_sources.py](../../src/buscador_midias/strategy/section_sources.py) | Fontes por seção: de cada seção do site, o feed ou sitemap que a serve. |
| [structural_recovery.py](../../src/buscador_midias/strategy/structural_recovery.py) | Saneamento conservador de candidatas estruturais refutadas por leitura anterior. |
| [structural_report.py](../../src/buscador_midias/strategy/structural_report.py) | Balanço estrutural do inventário, independente de notícias e mecanismos de coleta. |

### Entradas da aplicação

| Arquivo | Uso |
|---|---|
| [__main__.py](../../src/buscador_midias/__main__.py) | Entrada de `python -m buscador_midias` |
| [cli.py](../../src/buscador_midias/cli.py) | CLI geral, caminhos atuais e históricos; ingestão delega ao runtime |
| [criteria.py](../../src/buscador_midias/criteria.py) | Critério compartilhado Python/SQL de texto e entidades |

## Interface e recursos empacotados

| Arquivo | Uso |
|---|---|
| [manager.html](../../src/buscador_midias/dashboard/manager.html) | CRUD e navegação de fontes/mecanismos/saúde (`/gerenciador`, `/mecanismos`, `/saude`) |
| [health.js](../../src/buscador_midias/dashboard/health.js) | Rotinas e histórico de saúde |
| [guide.html](../../src/buscador_midias/dashboard/guide.html) | Guia didático e esquemas |
| [search.html](../../src/buscador_midias/dashboard/search.html) | Estrutura da tela de busca |
| [search.js](../../src/buscador_midias/dashboard/search.js) | Rodadas, configuração, filas, sites e corpus |
| [evidence.html](../../src/buscador_midias/dashboard/evidence.html) | Visualizador isolado de evidência |
| [evidence.js](../../src/buscador_midias/dashboard/evidence.js) | Carregamento e apresentação da captura |
| [evidence_prepare.js](../../src/buscador_midias/dashboard/evidence_prepare.js) | Preparação segura do HTML para legibilidade |
| [labeling/index.html](../../src/buscador_midias/labeling/index.html) | Avaliação manual/rotulagem da etapa anterior |
| [app.css](../../src/buscador_midias/dashboard/app.css) | Tokens, casca, componentes e estados de carregamento compartilhados por todas as telas (sempre claro) |
| [app.js](../../src/buscador_midias/dashboard/app.js) | `window.UI`: barra de progresso, botão ocupado e esqueletos de carregamento; envolve o `fetch` |
| [panel.html](../../src/buscador_midias/dashboard/panel.html) | Estrutura da tela Painel (`/`) |
| [panel.js](../../src/buscador_midias/dashboard/panel.js) | Painel: lê `/busca/painel` e desenha os gráficos em SVG, com dica, teclado e visão em tabela |

## Scripts

As auditorias são finitas e frequentemente vinculadas a caminhos, datas e manifestos específicos. Os nomes `apply`, `recover`, `repair`, `validate` e as opções `--apply` merecem conferir o código antes da execução. Não são rotinas que o painel agenda automaticamente.

| Arquivo | Finalidade |
|---|---|
| [apply_editorial_priority.py](../../scripts/apply_editorial_priority.py) | Aplica provas editoriais recentes da rodada, com revisão e auditoria por fonte. |
| [apply_transport_recovery.py](../../scripts/apply_transport_recovery.py) | Restaura apenas provas textuais independentes, com transporte e revisão auditados. |
| [compact_old_evidence.py](../../scripts/compact_old_evidence.py) | Compacta cópias HTML antigas sem alterar chaves, bytes ou registros do corpus. |
| [eval_evidence_reuse.py](../../scripts/eval_evidence_reuse.py) | Mede se o HTML do download direto serve de evidência: confronta com a cópia do Bright Data. |
| [export_sites.py](../../scripts/export_sites.py) | Exporta o catálogo de fontes confiáveis do seed do powerdd-backend para `data/sites.csv`. |
| [map_editorial_priority.py](../../scripts/map_editorial_priority.py) | Auditoria finita, HTTP e sem IA, de veículos editoriais prioritários. |
| [map_feeder_candidates.py](../../scripts/map_feeder_candidates.py) | Descobre RSS no diretório público do Feeder e comprova notícia/evidência antes de ativar. |
| [map_priority_sources.py](../../scripts/map_priority_sources.py) | Rodada finita de descoberta e prova textual dos 60 veículos selecionados. |
| [pilot_http_transports.py](../../scripts/pilot_http_transports.py) | Piloto finito de transporte em 20 URLs públicas, sem alterar catálogo ou ingestão. |
| [priority_catalog_apply.py](../../scripts/priority_catalog_apply.py) | Aplica apenas provas textuais da rodada dirigida, preservando exclusões manuais. |
| [probe_feeds.py](../../scripts/probe_feeds.py) | Mede quantas fontes oferecem feed RSS/Atom ou API do WordPress, como alternativa ao sitemap. |
| [probe_http_sessions.py](../../scripts/probe_http_sessions.py) | Amostra finita de acesso público: não altera o catálogo nem inicia ingestão/IA. |
| [probe_priority_news_evidence.py](../../scripts/probe_priority_news_evidence.py) | Prova finita de descoberta, texto e evidência; não altera catálogo ou corpus. |
| [recheck_canonical_transports.py](../../scripts/recheck_canonical_transports.py) | Uma conferência finita dos sete cadastros canônicos ainda no transporte padrão. |
| [recover_all_transports.py](../../scripts/recover_all_transports.py) | Rodada única dos excluídos automáticos e falhas de descoberta, com até três perfis curl. |
| [recover_excluded_sessions.py](../../scripts/recover_excluded_sessions.py) | Rodada finita dos excluídos originais com sessão HTTP e restauração apenas textual. |
| [report_feeder_mapping.py](../../scripts/report_feeder_mapping.py) | Consolida a rodada Feeder sem novas chamadas HTTP ou alterações no catálogo. |
| [report_transport_recovery.py](../../scripts/report_transport_recovery.py) | Consolida a auditoria finita e confere as restaurações no PostgreSQL, sem nova busca. |
| [resume_feeder_candidates.py](../../scripts/resume_feeder_candidates.py) | Conclui exclusivamente os candidatos Feeder pendentes, sem repetir os testes anteriores. |
| [retest_excluded_direct.py](../../scripts/retest_excluded_direct.py) | Reteste finito de exclusões por robots_unavailable, sem alterar o catálogo. |
| [review_catalog.py](../../scripts/review_catalog.py) | Revisa o catálogo inteiro; --apply executa apenas consolidações explícitas e auditadas. |
| [validate_catalog.py](../../scripts/validate_catalog.py) | Valida todos os links atuais, com retomada e no máximo três tentativas por associação. |
| [validate_recovered_direct.py](../../scripts/validate_recovered_direct.py) | Validação textual finita e restauração auditada dos XMLs recuperados via HTTP. |
| [bm.sh](../../scripts/bm.sh) | Wrapper de ambiente e CLI; uso operacional atual |
| [run.sh](../../scripts/run.sh) | Wrapper do fluxo CLI antigo |
| [install_cron.sh](../../scripts/install_cron.sh) | Instala/remove cron do fluxo antigo; não é o scheduler Rabbit |
| [generate_canonical_golden.php](../../scripts/generate_canonical_golden.php) | Gera fixture de paridade da canonicalização PHP/Python |
| [apply_mapping.py](../../scripts/apply_mapping.py) | Aplica ao catálogo as propostas da rodada de mapeamento, em duas etapas retomáveis. |
| [backfill_read_order.py](../../scripts/backfill_read_order.py) | Cria a coluna `links.read_order` (se faltar) e a preenche dos documentos já em cache. |
| [consolidate_site_links.py](../../scripts/consolidate_site_links.py) | Exclui (exclusão lógica) os links de um site cujo conteúdo já está nos outros links dele. |
| [coverage_gap_routes.py](../../scripts/coverage_gap_routes.py) | Uma correção limitada de rotas oficiais, separada da cobertura da rodada original. |
| [head_filter_report.py](../../scripts/head_filter_report.py) | Resume o corte pelo topo (`head_filter`) de uma rodada: o que mediu e se já dá para ativar. |
| [map_principal_sites.py](../../scripts/map_principal_sites.py) | Rodada finita de mapeamento dos principais sites, com Bright Data limitado por site. |

## Testes por arquivo

A seleção abaixo é um mapa, não um relatório de execução. Suítes de PostgreSQL/Rabbit e a fixture global têm efeitos de infraestrutura descritos em [Operação](OPERACAO.md).

### `tests`

- [conftest.py](../../tests/conftest.py): Banco de testes: um schema novo por teste, criado com o mesmo DDL da migration.
- [test_cli.py](../../tests/test_cli.py): Exemplos de contratos: `test_parser_tem_padroes_conservadores`, `test_bright_data_e_opt_in_com_teto_padrao`, `test_bright_data_sem_credenciais_aborta_antes_de_qualquer_requisicao`.
- [test_cli_corpus.py](../../tests/test_cli_corpus.py): Exemplos de contratos: `test_parser_tem_padroes`, `test_grava_os_achados_e_imprime_o_resumo`, `test_dry_run_so_conta`.
- [test_cli_discover.py](../../tests/test_cli_discover.py): Exemplos de contratos: `test_parser_do_discover_tem_padroes_conservadores`, `test_primeira_rodada_e_linha_de_base_e_a_segunda_acha_as_novas`, `test_estado_persiste_entre_execucoes_no_arquivo`.
- [test_cli_enrich.py](../../tests/test_cli_enrich.py): Exemplos de contratos: `test_parser_do_enrich_tem_padroes_baratos_e_reaproveita_o_html`, `test_enrich_analisa_e_reaproveita_o_html_do_download_direto`, `test_modo_brightdata_usa_so_o_unlocker`.
- [test_cli_export_strategies.py](../../tests/test_cli_export_strategies.py): Exemplos de contratos: `test_parser_do_export_tem_saida_padrao_em_xlsx`, `test_exporta_todas_as_fontes_do_catalogo_inclusive_as_nao_sondadas`, `test_exporta_csv_pelo_sufixo_e_so_as_fontes_pedidas`.
- [test_cli_mapping.py](../../tests/test_cli_mapping.py): Exemplos de contratos: `test_comando_mapeia_retoma_e_exporta_sem_escrever_urls_do_corpus`, `test_rejeita_orcamento_invalido_antes_de_acessar_rede`, `test_rejeita_limites_totais_invalidos_sem_http`.
- [test_cli_migrate.py](../../tests/test_cli_migrate.py): Exemplos de contratos: `test_parser_do_migrate_exige_o_arquivo`, `test_migra_e_imprime_quantas_linhas_foram_por_tabela`, `test_segunda_migracao_para_o_mesmo_destino_e_recusada`.
- [test_cli_probe_sources.py](../../tests/test_cli_probe_sources.py): Exemplos de contratos: `test_parser_tem_padroes_conservadores`, `test_grava_o_principal_e_a_reserva_da_fonte_que_serve`, `test_fonte_sem_estrategia_aparece_com_o_motivo_e_nao_apaga_a_anterior`.
- [test_cli_run.py](../../tests/test_cli_run.py): Exemplos de contratos: `test_parser_do_run_tem_padroes_do_fluxo_agendado`, `test_run_descobre_analisa_confere_o_criterio_captura_evidencia_e_grava_no_corpus`, `test_etapa_que_falha_nao_impede_as_seguintes_e_o_codigo_de_saida_avisa`.
- [test_cli_strategy_health.py](../../tests/test_cli_strategy_health.py): Relatório de quebras: quais fontes tiveram o padrão de leitura quebrado.
- [test_criteria.py](../../tests/test_criteria.py): Exemplos de contratos: `test_materia_lida_com_entidade_atende`, `test_materia_lida_sem_entidade_nao_atende`, `test_analise_so_pela_manchete_nao_atende_mesmo_com_entidade`.
- [test_install_cron_script.py](../../tests/test_install_cron_script.py): Exemplos de contratos: `test_instala_uma_linha_por_horario_chamando_o_run_sh`, `test_minutos_vao_para_o_campo_certo`, `test_grava_a_agenda_do_painel_com_os_mesmos_horarios`.
- [test_postgres_schema.py](../../tests/test_postgres_schema.py): O DDL do Postgres (`sql/postgres`) é a fonte do esquema do estado do buscador.
- [test_recover_excluded_sessions.py](../../tests/test_recover_excluded_sessions.py): Rodada dos excluídos: limites, pausas e comprovação textual antes de restaurar.
- [test_retest_excluded_direct.py](../../tests/test_retest_excluded_direct.py): Reteste HTTP isolado: sem rede, corpus ou serviços pagos.
- [test_run_script.py](../../tests/test_run_script.py): Exemplos de contratos: `test_repassa_os_argumentos_ao_comando_run`, `test_carrega_so_as_credenciais_necessarias_e_tira_as_aspas`, `test_o_corpus_e_o_banco_local_por_padrao_e_pode_ser_trocado`.
- [test_validate_recovered_direct.py](../../tests/test_validate_recovered_direct.py): Validação de recuperados: texto é obrigatório e rede não usa robots nem Bright.

### `tests/analysis`

- [test_context.py](../../tests/analysis/test_context.py): Exemplos de contratos: `test_mensagem_traz_cada_item_em_json_de_uma_linha_com_o_texto_truncado`, `test_base_somente_manchete_e_sinalizada_ao_modelo`, `test_texto_com_quebras_de_linha_nao_quebra_o_formato`.
- [test_headline.py](../../tests/analysis/test_headline.py): Exemplos de contratos: `test_tokens_sem_acento_minusculos_e_sem_numeros`, `test_tokens_ignora_letra_solta_e_id_colado_em_digitos`, `test_sufixo_de_veiculo_so_sai_quando_e_curto_e_sobra_manchete`.
- [test_mechanism_metrics.py](../../tests/analysis/test_mechanism_metrics.py): Exemplos de contratos: `test_exclusividade_nao_confunde_ocorrencias_com_links_distintos`, `test_simulacao_reaproveita_documentos_e_pula_raiz_sem_ganho`, `test_custo_ausente_nao_vira_custo_zero`.
- [test_mechanism_selection.py](../../tests/analysis/test_mechanism_selection.py): Exemplos de contratos: `test_sem_novidades_nao_exclui_xml_funcional`, `test_audio_isolado_em_sitemap_geral_nao_exclui_mecanismo`, `test_xml_podcast_comprovado_e_amostra_audio_autorizam_exclusao_da_selecao`.
- [test_openrouter.py](../../tests/analysis/test_openrouter.py): Exemplos de contratos: `test_chamada_traz_modelo_temperatura_zero_prompt_e_mensagem`, `test_uso_e_custo_sao_acumulados_entre_chamadas`, `test_sem_custo_informado_o_total_fica_nulo`.
- [test_replies.py](../../tests/analysis/test_replies.py): Exemplos de contratos: `test_aceita_objeto_com_items`, `test_aceita_array_json_puro_na_raiz`, `test_aceita_bloco_de_codigo_e_texto_em_volta`.

### `tests/corpus`

- [test_article.py](../../tests/corpus/test_article.py): Exemplos de contratos: `test_nome_normalizado_como_o_kyx_ascii_minusculo_e_espacos_unicos`, `test_tipo_da_entidade_usa_o_vocabulario_do_corpus`, `test_papeis_usam_o_vocabulario_do_corpus`.
- [test_country.py](../../tests/corpus/test_country.py): Exemplos de contratos: `test_country_precedence_and_unknowns`, `test_registry_prefers_specific_host_and_path_without_matching_newsletter`, `test_registry_without_covering_path_requires_agreement`.
- [test_index.py](../../tests/corpus/test_index.py): Exemplos de contratos: `test_known_hashes_devolve_so_os_que_existem`, `test_consulta_usa_any_com_lista_e_a_tabela_do_corpus`, `test_entrada_vazia_nao_consulta_o_banco`.
- [test_sync.py](../../tests/corpus/test_sync.py): Exemplos de contratos: `test_grava_os_achados_pendentes_e_registra_cada_gravacao`, `test_o_artigo_gravado_leva_nome_da_fonte_pais_e_texto`, `test_segunda_execucao_sem_novidade_nao_grava_nada`.
- [test_writer_pg.py](../../tests/corpus/test_writer_pg.py): Testes do gravador contra um Postgres real, em um schema temporário (somem ao final).

### `tests/dashboard`

- [test_access_audit.py](../../tests/dashboard/test_access_audit.py): Exemplos de contratos: `test_uniao_deduplica_fonte_e_nao_confunde_xml_com_acesso`, `test_nao_expoe_links_estranhos_e_respeita_exclusoes`, `test_ausencia_de_arquivos_e_releitura_de_exportacao_nova`.
- [test_catalog_batch.py](../../tests/dashboard/test_catalog_batch.py): Janela anual, fila finita e aplicação auditada das decisões em dados simulados.
- [test_catalog_bright_reader.py](../../tests/dashboard/test_catalog_bright_reader.py): Fallback do gerenciador preserva escopo, orçamento e status do destino.
- [test_catalog_health.py](../../tests/dashboard/test_catalog_health.py): Política de saúde distingue acesso, ausência de notícias e formato inválido.
- [test_catalog_health_postgres.py](../../tests/dashboard/test_catalog_health_postgres.py): Filas, retomada, configurações e testes reais em PostgreSQL isolado.
- [test_catalog_manager.py](../../tests/dashboard/test_catalog_manager.py): Cadastro real persistente, estados herdados, evidências e API local protegida.
- [test_catalog_postgres.py](../../tests/dashboard/test_catalog_postgres.py): Migração e CRUD reais em esquema descartável da base PostgreSQL de teste.
- [test_catalog_schema.py](../../tests/dashboard/test_catalog_schema.py): Migração local preserva todos os dados e impede relacionamentos órfãos.
- [test_catalog_testing.py](../../tests/dashboard/test_catalog_testing.py): Testes reais dos modelos com rede simulada, persistência e limites explícitos.
- [test_content_filter.py](../../tests/dashboard/test_content_filter.py): Filtro de conteúdo rotineiro: regras estreitas, com proteção contra perda de notícia adversa.
- [test_curl_transport.py](../../tests/dashboard/test_curl_transport.py): Exemplos de contratos: `test_default_reader_is_unchanged_and_unknown_profile_is_rejected`, `test_h2_profile_pins_public_ip_and_reports_actual_protocol`, `test_h1_bot_profile_keeps_bot_identity`.
- [test_editorial_mapping.py](../../tests/dashboard/test_editorial_mapping.py): Exemplos de contratos: `test_recent_proof_requires_article_text_and_publication_within_window`, `test_fixed_archive_leaf_does_not_become_daily_route_even_with_recent_news`, `test_huge_generic_index_requires_review_but_g1_has_an_explicit_daily_plan`.
- [test_feeder_mapping.py](../../tests/dashboard/test_feeder_mapping.py): Procedência e deduplicação dos candidatos de um diretório externo.
- [test_functional.py](../../tests/dashboard/test_functional.py): Contagens sem corte de data e consulta funcional sem banco ou coleta.
- [test_g1_daily.py](../../tests/dashboard/test_g1_daily.py): Seleção explícita do g1 não depende de varrer o arquivo histórico.
- [test_http_session.py](../../tests/dashboard/test_http_session.py): Sessão pública, keep-alive, espera do servidor e ciclos sem chamadas ocultas.
- [test_http_transport_pilot.py](../../tests/dashboard/test_http_transport_pilot.py): Exemplos de contratos: `test_http_200_challenge_is_not_a_valid_feed_or_article`, `test_xml_success_does_not_claim_article_text_without_an_article_request`, `test_access_failure_does_not_confirm_text_even_with_article_like_body`.
- [test_ingestion.py](../../tests/dashboard/test_ingestion.py): Filas de ingestão em PostgreSQL isolado: sem chamadas pagas ou corpus real.
- [test_ingestion_content_filter.py](../../tests/dashboard/test_ingestion_content_filter.py): Barreiras de conteúdo rotineiro no fluxo: URL, título e categoria da análise de contexto.
- [test_ingestion_daily.py](../../tests/dashboard/test_ingestion_daily.py): Janela civil e roteamento sem expansão de arquivos sem indicação temporal.
- [test_ingestion_evidence_view.py](../../tests/dashboard/test_ingestion_evidence_view.py): Visualização lê a cópia salva e não aceita arquivos fora da raiz de evidências.
- [test_ingestion_workers.py](../../tests/dashboard/test_ingestion_workers.py): Empréstimo de capacidade entre filas, sem conexão ao RabbitMQ.
- [test_mapping.py](../../tests/dashboard/test_mapping.py): Exemplos de contratos: `test_catalogo_inclui_fontes_sem_mapa_e_identifica_duplicatas`, `test_secao_traz_exemplo_seguro_e_data_sem_injetar_titulo`, `test_paginacao_busca_e_lacunas_nao_omitem_dados`.
- [test_priority_mapping.py](../../tests/dashboard/test_priority_mapping.py): Gravação real da rodada dirigida em esquema PostgreSQL descartável.
- [test_priority_probe.py](../../tests/dashboard/test_priority_probe.py): Provas de data e segregação editorial do teste finito de evidências.
- [test_recovered_direct_postgres.py](../../tests/dashboard/test_recovered_direct_postgres.py): Aplicação transacional da recuperação em schema PostgreSQL isolado.
- [test_round_view.py](../../tests/dashboard/test_round_view.py): Exemplos de contratos: `test_tela_simples_e_resumo_isolam_rodada_12`, `test_links_filtram_rodada_janela_datas_futuras_e_descartes`, `test_encerramento_parcial_preserva_fonte_incompleta_sem_simular_conclusao`.
- [test_schedule.py](../../tests/dashboard/test_schedule.py): Exemplos de contratos: `test_arquivo_ausente_ou_invalido_nao_derruba_nada`, `test_none_e_sem_agenda`, `test_agenda_do_arquivo_ordena_e_calcula_a_contagem_regressiva`.
- [test_selection.py](../../tests/dashboard/test_selection.py): Exemplos de contratos: `test_selecao_funciona_sem_banco_e_preserva_custo_ausente`, `test_filtros_paginacao_e_downloads_permitidos`, `test_manifesto_atualiza_retrato_e_pacote_ausente_informa_indisponibilidade`.
- [test_server.py](../../tests/dashboard/test_server.py): Exemplos de contratos: `test_pagina_inicial_e_html_com_cabecalhos_de_seguranca`, `test_api_devolve_o_snapshot_em_json_sem_cache`, `test_parametros_hours_e_recent_sao_limitados`.
- [test_snapshot.py](../../tests/dashboard/test_snapshot.py): Exemplos de contratos: `test_snapshot_e_serializavel_em_json`, `test_funil_acumulado_soma_os_tipos_e_as_que_aguardam_analise`, `test_status_ocioso_quando_a_ultima_rodada_terminou`.
- [test_transport_confirmation_cache.py](../../tests/dashboard/test_transport_confirmation_cache.py): Provas compartilhadas têm fonte/perfil/URL e validade restritos, sem sessões importadas.
- [test_transport_postgres.py](../../tests/dashboard/test_transport_postgres.py): Exemplos de contratos: `test_automatic_restore_persists_profile_and_original_history`, `test_manual_deletion_and_source_edits_are_preserved`, `test_snapshot_and_reader_keep_the_configured_profile`.
- [test_early_stop_reader.py](../../tests/dashboard/test_early_stop_reader.py): Leitura parcial: o leitor entrega o prefixo ao chamador, que pode dispensar o resto.
- [test_head_cut.py](../../tests/dashboard/test_head_cut.py): Corte pelo topo: com ordem `newest_first` comprovada, topo fora da janela dispensa o resto.
- [test_health_probe.py](../../tests/dashboard/test_health_probe.py): Rotina de saúde com as regras novas: sonda leve nos ativos e portão de frescor nos inativos.
- [test_health_rules.py](../../tests/dashboard/test_health_rules.py): Regras da rotina de saúde: ação por resultado da sonda e próximo horário fixo.
- [test_health_schedule.py](../../tests/dashboard/test_health_schedule.py): Rotina de saúde por horários fixos, com modo e escopo configuráveis.
- [test_ingestion_cold_links.py](../../tests/dashboard/test_ingestion_cold_links.py): Leitura adaptativa: link que não rende há várias rodadas é lido menos, nunca abandonado.
- [test_ingestion_day_close.py](../../tests/dashboard/test_ingestion_day_close.py): Fechamento do dia e nova tentativa de falhas: nada publicado depois da última rodada se perde.
- [test_ingestion_entity_contexts.py](../../tests/dashboard/test_ingestion_entity_contexts.py): Contextos em lotes retomáveis: trinta entidades, resposta parcial e reserva substituída.
- [test_ingestion_head_filter.py](../../tests/dashboard/test_ingestion_head_filter.py): Corte pelo topo na descoberta: sombra só mede, ativo dispensa o documento inteiro.
- [test_ingestion_panel.py](../../tests/dashboard/test_ingestion_panel.py): Dados do painel: última rodada, histórico, agora, falhas, saúde e auditoria.
- [test_ingestion_report_indexes.py](../../tests/dashboard/test_ingestion_report_indexes.py): Índices por rodada e painel de rodadas: as telas contam por run_id sem varrer o histórico.
- [test_ingestion_run_stats.py](../../tests/dashboard/test_ingestion_run_stats.py): Estatísticas pré-agregadas por rodada: o painel lê números prontos, não varre as tarefas.
- [test_ingestion_source_progress_cache.py](../../tests/dashboard/test_ingestion_source_progress_cache.py): Aba "Por site": virar página ou buscar não recalcula todas as fontes da rodada.
- [test_link_freshness.py](../../tests/dashboard/test_link_freshness.py): Frescor de um link: a data mais nova que o próprio feed ou sitemap declara.
- [test_link_period.py](../../tests/dashboard/test_link_period.py): Período coberto pelo endereço de um link: fatia de dia ou mês que já fechou é histórica.
- [test_link_read_order_store.py](../../tests/dashboard/test_link_read_order_store.py): A ordem de leitura vive no link: vem do teste, pode ser corrigida e some se o link muda.
- [test_link_redundancy.py](../../tests/dashboard/test_link_redundancy.py): Links de um mesmo site cujo conteúdo já está inteiro em outros são redundantes.
- [test_link_review.py](../../tests/dashboard/test_link_review.py): Sonda de um link: o que o cadastro deveria fazer com ele, sem alterar nada.
- [test_link_review_cli.py](../../tests/dashboard/test_link_review_cli.py): Subcomando review-links: probe grava o relatório e apply só mexe no que foi escolhido.
- [test_link_theme.py](../../tests/dashboard/test_link_theme.py): Tema inútil de um link, pelo caminho da URL e pelo rótulo cadastrado.
- [test_mapping_apply.py](../../tests/dashboard/test_mapping_apply.py): Aplicação das propostas de mapeamento: só entra link provado, vivo e com tema útil.
- [test_read_order.py](../../tests/dashboard/test_read_order.py): Ordem em que um documento lista suas entradas: de onde vem o corte seguro.
- [test_read_order_backfill.py](../../tests/dashboard/test_read_order_backfill.py): Preenche a ordem de leitura dos links a partir dos documentos já em cache, sem rede.
- [test_site_budget.py](../../tests/dashboard/test_site_budget.py): Teto de chamadas pagas por site, somado entre todas as leituras daquele site.

### `tests/discovery`

- [test_discover_all.py](../../tests/discovery/test_discover_all.py): Exemplos de contratos: `test_rodada_cria_run_grava_resultado_por_site_e_fecha_a_run`, `test_segunda_rodada_junta_as_novas_de_todos_os_sites_na_ordem_dos_sites`, `test_falha_inesperada_em_um_site_nao_derruba_os_demais`.
- [test_document_queue.py](../../tests/discovery/test_document_queue.py): Exemplos de contratos: `test_alterna_fontes_e_nao_ocupa_worker_com_host_em_uso`, `test_checkpoint_reabre_so_ativas_e_nao_repete_documento_confirmado`.
- [test_explore.py](../../tests/discovery/test_explore.py): Exemplos de contratos: `test_janela_ignora_arquivo_antigo_e_preserva_filho_sem_data`, `test_janela_prefere_publicacao_e_nao_chama_sem_data_de_recente`, `test_sem_teto_le_todos_os_filhos_inclusive_taxonomias_e_encerra_ciclo`.
- [test_feed_explore.py](../../tests/discovery/test_feed_explore.py): Exemplos de contratos: `test_feed_antigo_e_feed_vazio_sao_acessiveis_sem_resultados_recentes`, `test_le_o_feed_declarado_na_home`, `test_sem_feed_declarado_adivinha_os_caminhos_e_para_no_primeiro_que_serve`.
- [test_feeds.py](../../tests/discovery/test_feeds.py): Exemplos de contratos: `test_acha_os_feeds_declarados_na_home_com_url_absoluta`, `test_ignora_o_que_nao_e_feed_de_noticias`, `test_rel_com_varios_valores_atributos_em_caixa_alta_e_duplicatas`.
- [test_folha_feed.py](../../tests/discovery/test_folha_feed.py): Exemplos de contratos: `test_official_folha_redirect_preserves_destination_and_publication`, `test_does_not_unwrap_unrelated_or_unsafe_targets`, `test_does_not_rewrite_other_publishers_or_arbitrary_redirectors`.
- [test_incremental.py](../../tests/discovery/test_incremental.py): Exemplos de contratos: `test_xml_acessivel_so_com_noticias_antigas_e_sucesso_sem_recentes`, `test_publicacao_do_complemento_prevalece_sobre_lastmod_antigo`, `test_checkpoint_dos_filhos_do_complemento_e_preservado_entre_rodadas`.
- [test_probe.py](../../tests/discovery/test_probe.py): Exemplos de contratos: `test_ok_via_index_conta_urls_recentes`, `test_fallback_para_sitemap_xml_quando_robots_nao_declara`, `test_robots_ausente_ainda_tenta_os_caminhos_padrao`.
- [test_report.py](../../tests/discovery/test_report.py): Exemplos de contratos: `test_summarize_conta_por_status_e_capacidades`, `test_write_probe_report_gera_csv_legivel`, `test_probe_all_avisa_o_progresso_a_cada_site_concluido`.
- [test_robots.py](../../tests/discovery/test_robots.py): Exemplos de contratos: `test_extrai_sitemaps_declarados_ignorando_caixa`, `test_respeita_disallow_do_agente_curinga`, `test_respeita_regra_especifica_do_agente`.
- [test_sitemap_parser.py](../../tests/discovery/test_sitemap_parser.py): Exemplos de contratos: `test_urlset_extrai_urls_e_datas`, `test_index_extrai_sitemaps_filhos`, `test_news_sitemap_traz_titulo_e_data_de_publicacao`.
- [test_sources.py](../../tests/discovery/test_sources.py): Exemplos de contratos: `test_fonte_sitemap_devolve_as_urls_da_fonte_sem_repetir`, `test_fonte_sitemap_com_falha_traz_o_motivo_e_nenhuma_url`, `test_fonte_de_feed_vira_urls_com_titulo_e_data_de_publicacao`.
- [test_stats.py](../../tests/discovery/test_stats.py): Exemplos de contratos: `test_resumo_por_rodada_soma_os_sites`, `test_sites_sem_novidade_exigem_ao_menos_uma_rodada_alem_da_linha_de_base`, `test_site_so_com_linha_de_base_ainda_nao_pode_ser_julgado`.
- [test_strategy_set.py](../../tests/discovery/test_strategy_set.py): Exemplos de contratos: `test_sem_tetos_le_todos_os_complementos_alem_do_orcamento_padrao`, `test_principal_e_complemento_sao_unidos_sem_ler_reserva_ou_fallback`, `test_falha_de_complemento_preserva_principal_e_indica_leitura_parcial`.
- [test_url_filter.py](../../tests/discovery/test_url_filter.py): Exemplos de contratos: `test_materias_passam`, `test_pagina_inicial`, `test_paginas_de_listagem`.
- [test_xml_review.py](../../tests/discovery/test_xml_review.py): Exemplos de contratos: `test_indice_apenas_devolve_referencias_inclusive_ciclo_sem_expandir`, `test_feed_antigo_continua_elegivel_e_audio_tem_evidencia_estrutural`, `test_html_nao_vira_xml_funcional`.

### `tests/domain`

- [test_dated_url.py](../../tests/domain/test_dated_url.py): URLs com data embutida (um sitemap por dia ou por mês): data, modelo e renderização.
- [test_discovered.py](../../tests/domain/test_discovered.py): Exemplos de contratos: `test_from_entry_calcula_o_hash_canonico_e_guarda_a_origem`, `test_timestamp_prefere_a_publicacao_e_cai_no_lastmod`.
- [test_legacy_hash.py](../../tests/domain/test_legacy_hash.py): Hash legado: o corpus tem linhas hasheadas antes do commit 413fc198f do KyX (2026-10-01).
- [test_site.py](../../tests/domain/test_site.py): Exemplos de contratos: `test_host_remove_www`, `test_host_preserva_www_numerado`, `test_url_preserva_www_e_esquema_para_o_fetch`.
- [test_url.py](../../tests/domain/test_url.py): Paridade com o MidiasIaUrlCanonicalizer do KyX.

### `tests/enrich`

- [test_enrich_pipeline.py](../../tests/enrich/test_enrich_pipeline.py): Exemplos de contratos: `test_fluxo_completo_texto_analise_e_evidencia_do_achado`, `test_o_modelo_recebe_titulo_da_pagina_descricao_e_texto`, `test_sem_texto_a_analise_usa_so_a_manchete_e_nao_gasta_evidencia`.

### `tests/evidence`

- [test_capture.py](../../tests/evidence/test_capture.py): Exemplos de contratos: `test_html_do_download_direto_e_reaproveitado_quando_confiavel`, `test_reaproveitamento_rejeitado_cai_para_o_bright_data_e_registra_o_motivo`, `test_modo_somente_bright_data_ignora_o_html_direto`.
- [test_s3_storage.py](../../tests/evidence/test_s3_storage.py): Exemplos de contratos: `test_grava_no_bucket_com_o_mesmo_caminho_do_kyx_e_tipo_de_conteudo`, `test_prefixo_e_configuravel`, `test_hash_inseguro_e_recusado`.
- [test_storage.py](../../tests/evidence/test_storage.py): Exemplos de contratos: `test_grava_no_mesmo_caminho_relativo_do_kyx`, `test_prefixo_e_configuravel`, `test_regravar_substitui_o_conteudo_sem_deixar_arquivo_temporario`.

### `tests/extraction`

- [test_compare.py](../../tests/extraction/test_compare.py): Exemplos de contratos: `test_mesma_pagina_e_identica`, `test_paginas_diferentes_tem_baixa_similaridade_e_campos_diferentes`, `test_diferenca_pequena_de_texto_continua_muito_similar`.
- [test_fetch.py](../../tests/extraction/test_fetch.py): Exemplos de contratos: `test_artigo_baixado_traz_corpo_tipo_e_url_final`, `test_robots_que_proibe_impede_o_download`, `test_robots_e_baixado_uma_vez_por_site_mesmo_com_varios_artigos`.
- [test_html.py](../../tests/extraction/test_html.py): Exemplos de contratos: `test_titulo_prefere_og_title_depois_title_depois_h1`, `test_descricao_vem_do_og_ou_da_meta_description`, `test_texto_vem_dos_paragrafos_do_artigo_sem_menu_rodape_e_scripts`.
- [test_occrp.py](../../tests/extraction/test_occrp.py): Regressão do template público OCCRP: artigo e cards relacionados têm papéis distintos.
- [test_reliability.py](../../tests/extraction/test_reliability.py): Exemplos de contratos: `test_pagina_integra_e_confiavel`, `test_so_status_200_serve`, `test_tipo_de_conteudo_precisa_ser_html`.
- [test_sample_audit.py](../../tests/extraction/test_sample_audit.py): Exemplos de contratos: `test_data_de_modificacao_do_feed_nao_promove_artigo_antigo_a_recente`, `test_podcast_dedicado_nao_e_noticia_textual_mesmo_com_descricao_longa`, `test_artigo_com_audio_embutido_preserva_texto`.

### `tests/labeling`

- [test_cli_labels.py](../../tests/labeling/test_cli_labels.py): Exemplos de contratos: `test_parser_dos_rotulos_tem_padroes_razoaveis`, `test_export_grava_o_lote_e_diz_o_que_fazer`, `test_score_sem_rotulos_explica_o_que_falta`.
- [test_label_server.py](../../tests/labeling/test_label_server.py): Exemplos de contratos: `test_pagina_sai_com_cabecalhos_de_seguranca`, `test_itens_trazem_so_o_que_e_cego`, `test_voto_grava_no_csv_e_atualiza_o_progresso`.
- [test_labeling.py](../../tests/labeling/test_labeling.py): Exemplos de contratos: `test_lote_tem_os_dois_grupos_e_o_csv_nao_revela_o_veredito_do_modelo`, `test_chave_guarda_o_grupo_e_o_veredito_do_modelo`, `test_ordem_do_csv_e_embaralhada_para_nao_agrupar_por_veredito`.
- [test_session.py](../../tests/labeling/test_session.py): Exemplos de contratos: `test_lista_so_os_campos_cegos_na_ordem_do_arquivo`, `test_grava_o_veredito_no_csv_e_a_pontuacao_le`, `test_aceita_os_tres_vereditos_e_limpa_com_vazio`.
- [test_verdicts.py](../../tests/labeling/test_verdicts.py): Exemplos de contratos: `test_sinonimos_do_vocabulario_novo`, `test_palavras_do_vocabulario_antigo_nao_viram_sim_nem_nao`.

### `tests/net`

- [test_brightdata.py](../../tests/net/test_brightdata.py): Exemplos de contratos: `test_posta_no_endpoint_do_unlocker_com_token_zona_e_formato_raw`, `test_aceita_base_url_customizada_sem_barra_final`, `test_status_de_erro_da_api_passa_adiante_sem_conteudo`.
- [test_budget.py](../../tests/net/test_budget.py): Exemplos de contratos: `test_compartilha_saldo_e_nao_repete_http_apos_limite`, `test_prazo_e_exclusao_impedem_nova_chamada`, `test_zero_remove_tetos_mas_preserva_exclusoes`.
- [test_fallback.py](../../tests/net/test_fallback.py): Exemplos de contratos: `test_status_de_bloqueio_e_bloqueado`, `test_outros_status_nao_acionam_o_fallback`, `test_falha_de_transporte_nao_aciona_o_fallback`.
- [test_fetcher.py](../../tests/net/test_fetcher.py): Exemplos de contratos: `test_retorna_status_e_conteudo`, `test_envia_user_agent_configurado`, `test_cookie_nao_ascii_nao_interrompe_site_e_preserva_cookies_validos`.
- [test_limited.py](../../tests/net/test_limited.py): Exemplos de contratos: `test_repassa_ate_o_limite_e_depois_devolve_erro_sem_chamar_o_fetcher_interno`, `test_limite_vale_com_varias_threads`, `test_sem_limite_repassa_tudo`.
- [test_memoizing.py](../../tests/net/test_memoizing.py): Exemplos de contratos: `test_repete_a_resposta_sem_nova_requisicao_nos_hosts_compartilhados`, `test_hosts_fora_da_lista_nao_ficam_em_memoria`, `test_host_com_www_conta_como_o_host_da_lista`.
- [test_retry.py](../../tests/net/test_retry.py): Exemplos de contratos: `test_sucesso_na_primeira_nao_dorme`, `test_timeout_e_repetido_ate_dar_certo`, `test_5xx_transitorio_usa_espera_exponencial`.
- [test_stack.py](../../tests/net/test_stack.py): Exemplos de contratos: `test_sem_bright_data_nao_ha_fallback`, `test_bright_data_sem_credenciais_levanta_erro_que_nomeia_as_variaveis`, `test_site_bloqueado_e_buscado_pelo_unlocker_e_o_resto_continua_direto`.

### `tests/scripts`


### `tests/sites`

- [test_loader.py](../../tests/sites/test_loader.py): Exemplos de contratos: `test_carrega_sites_do_csv`, `test_deduplica_por_chave_mantendo_a_primeira`, `test_www_e_sem_www_sao_o_mesmo_site`.
- [test_seed_import.py](../../tests/sites/test_seed_import.py): Exemplos de contratos: `test_extrai_campos_relevantes_de_cada_linha`, `test_desfaz_escape_de_aspas_e_aceita_sede_nula`, `test_falha_alto_quando_alguma_linha_nao_e_reconhecida`.

### `tests/state`

- [test_migrate.py](../../tests/state/test_migrate.py): Exemplos de contratos: `test_migra_as_tabelas_com_os_tipos_do_postgres`, `test_preserva_ids_das_rodadas_e_a_ordem_de_chegada_das_urls`, `test_novas_rodadas_continuam_depois_do_maior_id_migrado`.
- [test_store.py](../../tests/state/test_store.py): Exemplos de contratos: `test_runs_tem_ids_crescentes_e_guardam_inicio_e_fim`, `test_record_urls_insere_novas_e_ignora_as_ja_vistas`, `test_a_primeira_classificacao_da_url_vence`.
- [test_store_counters.py](../../tests/state/test_store_counters.py): Exemplos de contratos: `test_contadores_acompanham_cada_insercao_por_fonte_e_tipo`, `test_url_repetida_nao_conta_duas_vezes_nem_em_outro_tipo`, `test_repeticao_dentro_do_mesmo_lote_conta_uma_vez`.
- [test_store_dashboard.py](../../tests/state/test_store_dashboard.py): Exemplos de contratos: `test_contagem_de_urls_por_tipo`, `test_urls_novas_por_hora_agrupam_pela_hora_de_descoberta`, `test_urls_novas_por_hora_ignoram_o_que_e_mais_antigo_que_o_limite`.
- [test_store_enrichment.py](../../tests/state/test_store_enrichment.py): Exemplos de contratos: `test_so_urls_novas_e_sem_analise_estao_pendentes`, `test_fila_de_analise_intercala_as_fontes_para_o_intervalo_por_host_nao_travar_o_bloco`, `test_falha_de_download_conta_tentativas_e_segue_pendente_ate_ter_analise`.
- [test_store_probe_state.py](../../tests/state/test_store_probe_state.py): Exemplos de contratos: `test_grava_e_le_o_estado_de_sondagem_e_a_regravacao_substitui`, `test_rodadas_recentes_vem_da_mais_nova_para_a_mais_antiga_com_o_inicio_da_rodada`, `test_rodadas_recentes_de_fonte_sem_rodada_nao_aparecem`.
- [test_store_strategies.py](../../tests/state/test_store_strategies.py): Exemplos de contratos: `test_grava_os_candidatos_e_devolve_com_a_medida_e_o_papel`, `test_a_nova_sondagem_substitui_as_linhas_da_fonte_e_so_dela`, `test_sondagem_sem_candidatos_limpa_a_fonte`.

### `tests/strategy`

- [helpers.py](../../tests/strategy/helpers.py): Apoio dos testes de estratégia: fetcher falso e construtores de sitemap, índice e feed.
- [test_discovery_plan.py](../../tests/strategy/test_discovery_plan.py): Exemplos de contratos: `test_carrega_plano_do_piloto_com_principal_e_complementos`, `test_rejeita_plano_invalido_antes_de_rede`.
- [test_evaluate.py](../../tests/strategy/test_evaluate.py): Avaliação de um endereço candidato: baixar, ler (sitemap, índice ou feed) e julgar.
- [test_export.py](../../tests/strategy/test_export.py): Planilha de conferência: o que se sabe de cada fonte, os candidatos e as seções.
- [test_health.py](../../tests/strategy/test_health.py): Quando reavaliar a estratégia de uma fonte: saúde das rodadas, idade e espera crescente.
- [test_hints.py](../../tests/strategy/test_hints.py): Dicas de endereços por fonte, vindas de um levantamento em CSV (palpite, nunca verdade).
- [test_listing.py](../../tests/strategy/test_listing.py): Varredura de listagens: da home às páginas de seção, atrás de links de matéria.
- [test_manual.py](../../tests/strategy/test_manual.py): Regras manuais por fonte: excluir um endereço ou forçar outro.
- [test_mapping.py](../../tests/strategy/test_mapping.py): Exemplos de contratos: `test_drain_consumes_new_subsections_without_revisiting_pages`, `test_drain_stops_on_unreadable_robots_preserving_the_pending_queue`, `test_drain_does_not_repeat_failures_between_batches`.
- [test_mapping_budget.py](../../tests/strategy/test_mapping_budget.py): Exemplos de contratos: `test_limite_total_nao_repassa_chamada_excedente`, `test_prazo_da_fonte_e_global_impedem_nova_chamada`.
- [test_mapping_coverage.py](../../tests/strategy/test_mapping_coverage.py): Exemplos de contratos: `test_valida_uma_fonte_uma_vez_e_compara_amostras_por_secao`, `test_audit_pauses_connection_failures_without_claiming_unattempted_endpoints`, `test_falha_de_feed_em_subdominio_nao_suspende_xml_de_outro_host`.
- [test_mapping_export.py](../../tests/strategy/test_mapping_export.py): Exemplos de contratos: `test_planilha_divide_abas_no_limite_do_excel_sem_perder_linhas`, `test_relatorio_nao_declara_cobertura_so_porque_a_fila_acabou`, `test_reaproveita_candidato_anterior_sem_inventar_a_secao_que_ele_cobre`.
- [test_mapping_priorities.py](../../tests/strategy/test_mapping_priorities.py): Exemplos de contratos: `test_prioriza_fonte_lida_sem_mecanismo_recente_e_distingue_feed_sem_data`, `test_mecanismo_recente_com_lacuna_tem_prioridade_sobre_amostras_correspondentes`, `test_feed_antigo_e_fora_do_escopo_nao_comprova_mecanismo_recente`.
- [test_mapping_review.py](../../tests/strategy/test_mapping_review.py): Exemplos de contratos: `test_falha_ao_ler_robots_nao_vira_proibicao_explicita`, `test_triagem_nao_sugere_retentar_falhas_permanentes_ou_limites`, `test_entrada_bem_sucedida_e_falha_em_subsecao_nao_entram_na_revisao_de_entradas`.
- [test_measure_and_scoring.py](../../tests/strategy/test_measure_and_scoring.py): Medição, veredito, nota e escolha das estratégias de descoberta de uma fonte.
- [test_mechanism_inventory.py](../../tests/strategy/test_mechanism_inventory.py): Exemplos de contratos: `test_endereco_de_maquina_pode_ficar_fora_do_prefixo_editorial`, `test_classifica_legibilidade_sem_prometer_atualizacao`, `test_associacao_separa_declaracao_correspondencia_e_leitura_historica`.
- [test_page_classification.py](../../tests/strategy/test_page_classification.py): Exemplos de contratos: `test_materia_com_slug_curto_e_schema_da_propria_url_nao_e_secao`, `test_og_article_isolado_como_no_m28_nao_rejeita_categoria`, `test_article_de_destaque_dentro_da_secao_nao_muda_o_tipo_da_pagina`.
- [test_probe_site.py](../../tests/strategy/test_probe_site.py): Sondagem de uma fonte: gera candidatos em etapas, avalia, pontua e escolhe.
- [test_section_sources.py](../../tests/strategy/test_section_sources.py): Fontes por seção: feeds declarados nas páginas, páginas-índice de feeds e padrões de URL.
- [test_structural_recovery.py](../../tests/strategy/test_structural_recovery.py): Exemplos de contratos: `test_descarta_palpite_refutado_preservando_evidencias`, `test_nao_apaga_lacuna_de_navegacao_entrada_ou_secao_confirmada`, `test_nao_descarta_falhas_transitorias_nem_candidatas_pendentes`.
- [test_structural_report.py](../../tests/strategy/test_structural_report.py): Exemplos de contratos: `test_entrada_sozinha_nao_comprova_estrutura`, `test_estrutura_examinada_exige_ausencia_de_pendencias_e_falhas`, `test_restricao_na_entrada_preserva_secoes_ja_mapeadas`.

## SQL, configuração e implantação

| Arquivo/diretório | Uso |
|---|---|
| [pyproject.toml](../../pyproject.toml) | Dependências, extras, recursos empacotados, pytest, Ruff e mypy |
| [sql/postgres](../../sql/postgres) | DDL histórico 001–008, transporte atual 009 e rollback 999 |
| [sql/corpus](../../sql/corpus) | Evolução de classificação do corpus existente |
| [rabbitmq.compose.yml](../../deploy/rabbitmq.compose.yml) | Broker próprio, portas locais e volume externo |
| [systemd](../../deploy/systemd/buscador-midias-ingest.service) | Exemplo de serviço de usuário, com caminhos desta instalação |
| [data](../../data) | Catálogo/hints/regras do levantamento anterior |

## Artefatos e exclusões do índice

`var/` contém resultados reais, provas, manifestos, checkpoints, logs e configurações locais. Seu conteúdo muda durante as rodadas e inclui dados não versionados. Consulte os manifestos das auditorias para reproduzir resultados; não tratar os números de um relatório como invariantes da aplicação. `.venv`, `__pycache__`, caches de ferramentas e credenciais não precisam ser publicados para documentar o produto.

## Entradas versionadas em data

| Arquivo | Finalidade |
|---|---|
| [sites.csv](../../data/sites.csv) | Catálogo histórico exportado de fontes; não reflete automaticamente edições do gerenciador |
| [estrategias_manuais.csv](../../data/estrategias_manuais.csv) | Intervenções explícitas na escolha/exclusão de estratégias do fluxo antigo |
| [hints_manuais.csv](../../data/hints_manuais.csv) | Endereços auxiliares conhecidos para sondagem |
| [discovery_hints.csv](../../data/discovery_hints.csv) | Dicas de caminhos do levantamento |
| [plano_descoberta.json](../../data/plano_descoberta.json) | Plano explícito de descoberta consumido pelo fluxo anterior |
| [poc_sample.txt](../../data/poc_sample.txt), [poc_sample_ext.txt](../../data/poc_sample_ext.txt) | Subconjuntos de fontes para a POC |
| [teste_g1.txt](../../data/teste_g1.txt), [teste_20_fontes.txt](../../data/teste_20_fontes.txt) | Recortes de fontes para pilotos |

## Componentes de otimização da ingestão

- [ingestion/batch.py](../../src/buscador_midias/ingestion/batch.py): finalização SQL de dedup em conjunto, preservando checkpoints e outbox por URL.
- [ingestion/connections.py](../../src/buscador_midias/ingestion/connections.py): pool limitado de transações curtas, separado das conexões com locks de rede.
- [ingestion/document_memo.py](../../src/buscador_midias/ingestion/document_memo.py): reutilização curta e limitada de respostas XML idênticas.
- [test_ingestion_fast_batch.py](../../tests/dashboard/test_ingestion_fast_batch.py): consistência, rollback, retomada e benchmark PostgreSQL isolado.
- [test_ingestion_document_memo.py](../../tests/dashboard/test_ingestion_document_memo.py): concorrência, isolamento, expiração e limites da reutilização HTTP.
- [ingestion/admission.py](../../src/buscador_midias/ingestion/admission.py): primeiro candidato pronto de cada host por salto no índice do outbox, com `host_concurrency` tarefas por host.
- [tests/dashboard/test_ingestion_stop_runs.py](../../tests/dashboard/test_ingestion_stop_runs.py): `ingest stop-runs`, cancela o trabalho não analisado, libera os artigos (`cancelled`) e fecha a rodada como `partial`.
- [tests/dashboard/test_ingestion_pool.py](../../tests/dashboard/test_ingestion_pool.py): tamanho dos pools de checkpoint (24) e de publicação (12).
- [tests/dashboard/test_ingestion_breaker.py](../../tests/dashboard/test_ingestion_breaker.py): disjuntor do provedor (5 falhas em 60 s, pausa de 60 s, etapas independentes).
- [tests/dashboard/test_ingestion_url_filter.py](../../tests/dashboard/test_ingestion_url_filter.py): `url_filter` (página de índice e dia do caminho anterior à janela), modos `off/shadow/enforce` e validação.
- [tests/dashboard/test_ingestion_publisher.py](../../tests/dashboard/test_ingestion_publisher.py): faxina periódica de obsoletos e hosts com trabalho novo por salto de índice.
- [ingestion/lastmod_backfill.py](../../src/buscador_midias/ingestion/lastmod_backfill.py): `ingest backfill-lastmod`, aplica o filtro por `lastmod` às tarefas já criadas.
- [test_ingestion_host_index.py](../../tests/dashboard/test_ingestion_host_index.py): coluna `host`, gatilho, seleção por host, vagas por host e trava de execução.
- [test_ingestion_lastmod.py](../../tests/dashboard/test_ingestion_lastmod.py): regra de `lastmod`, modos `off|shadow|enforce` e validação da configuração.
- [test_ingestion_lock_order.py](../../tests/dashboard/test_ingestion_lock_order.py): publicador e workers disputando as mesmas tarefas sem deadlock.
- [test_ingestion_schema_version.py](../../tests/dashboard/test_ingestion_schema_version.py): `initialize()` pula o DDL na versão atual e não espera locks da fila.
- [test_postgres_parameters.py](../../tests/dashboard/test_postgres_parameters.py): opções de sessão (sem paralelismo, limites de lock e de tempo).
- [corpus/theme_catalog.py](../../src/buscador_midias/corpus/theme_catalog.py) e `theme_catalog.json`: ligação slug da análise → `theme_id` do KyX, por tipo de pessoa.
- [corpus/theme_backfill.py](../../src/buscador_midias/corpus/theme_backfill.py): `ingest backfill-context-themes`.
- [test_theme_catalog.py](../../tests/corpus/test_theme_catalog.py) e [test_theme_backfill.py](../../tests/corpus/test_theme_backfill.py): catálogo e retroativo de tema.
- [test_ingestion_lastmod_backfill.py](../../tests/dashboard/test_ingestion_lastmod_backfill.py): relatório, aplicação idempotente e trava de segurança do retroativo.

## Auditoria de cobertura por capa

- [analysis/coverage.py](../../src/buscador_midias/analysis/coverage.py): referência de capa, elegibilidade temporal e métricas de cobertura.
- [audit_frontpage_coverage.py](../../scripts/audit_frontpage_coverage.py): espera a rodada exata encerrar e executa auditoria finita, com PostgreSQL somente leitura.
- [coverage_priority_sites.csv](../../data/coverage_priority_sites.csv): os 120 alvos explícitos.
- [test_frontpage_coverage.py](../../tests/coverage/test_frontpage_coverage.py): isolamento de janela, identidade, diagnóstico, espera e relatório com portas falsas.
