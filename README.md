# buscador-midias

Inventário de RSS/sitemaps das 1.294 fontes de notícias do catálogo para futuras buscas,
com auditoria dos mecanismos, associação às seções e lacunas explícitas.

## Consulta do mapa

O painel abre a tela de mapeamento em `http://127.0.0.1:8765`:

```bash
scripts/bm.sh dashboard
# Catálogo alternativo: scripts/bm.sh dashboard --sites caminho/sites.csv
```

A tela mostra os **1299 registros do catálogo**: 1294 fontes únicas e cinco duplicatas
identificadas. A visão inicial **Acesso às notícias** mostra o resultado da auditoria sem
filtro de data: **810 fontes** com página de notícia acessível e **777** com texto legível,
com **653** confirmadas por RSS e **656** por sitemap (há sobreposição entre esses grupos).
Ao selecionar uma fonte, aparecem os mecanismos, links de notícias testadas, resultado HTTP,
evidências de extração, limites e motivos de falha. Não testados aparecem explicitamente,
sem serem classificados como falhas. Há busca, filtros por país, situação da fonte, tipo
RSS/sitemap e resultado da auditoria, além de paginação e download da planilha completa.
As abas **RSS e sitemaps** e **Associação às seções** preservam o inventário funcional e
as associações observadas. Fontes sem mecanismos continuam visíveis.

As rotas `/` e `/mapa` servem esta tela; `/fluxo` preserva o painel operacional anterior.
`/api/mapa/fontes` consulta o catálogo e `/api/mapa/secoes?site=CHAVE` fornece as seções paginadas.
Essas consultas abrem o estado somente leitura e não disparam buscas nos sites.
`/api/mapa/acesso?site=CHAVE` lê os relatórios locais de `var/auditoria-acesso-sitemaps`
e `var/auditoria-acesso-rss`, com `kind=all|feed|sitemap`,
`mode=all|accessible|readable|unconfirmed|untested`, busca `q` e paginação `offset`/`limit`.
`/api/mapa/auditoria.xlsx` baixa a planilha consolidada. A API de acesso não abre o banco
nem faz requisições aos veículos. Sem os arquivos locais, informa que a auditoria está
indisponível; os arquivos não são gerados ao abrir a tela.

## RSS/sitemaps funcionais para buscas

O critério principal é **XML acessível e interpretado com sucesso na última auditoria**,
independentemente da idade ou quantidade de itens. Entram resultados `fresh`, `slow`,
`undated`, `stale`, `archive` e `empty`, em RSS/Atom, sitemap ou news sitemap.
Atualidade dos itens e URL com data fixa são informações separadas, sem excluir mecanismos.
Uma URL com data fixa serve ao período indicado; sua existência não comprova um índice
estável para atualizações futuras. A classificação é baseada em evidência armazenada,
sem revalidar a internet ao abrir a tela.

Não entram como funcionais HTML, feeds de comentários, respostas inválidas, falhas/bloqueios,
links assinados, credenciais na URL ou mecanismos excluídos manualmente. Um RSS pode ficar
fora do prefixo editorial da fonte no mesmo host; os links de notícias respeitam o escopo.

A aba **RSS e sitemaps** inicia em **Todos os RSS/sitemaps funcionais**, enquanto a tela
abre na aba de acesso às notícias. O inventário funcional exibe resultado da leitura, idade dos
itens, leitura parcial, exemplos e marca de data fixa. Há filtros opcionais de atualidade;
esses filtros não definem o total funcional nem o plano principal. O CSV de download segue
o critério funcional. A aba de seções preserva declarações e correspondências observadas
com até cinco exemplos por página, distinguindo auditorias anteriores à página. Isso não
comprova cobertura integral da seção.

Retrato recalculado em 18/10/2026: **999 fontes** com XML funcional, **295** sem leitura funcional
registrada. São 36.402 registros de mecanismos funcionais, ou **36.054** após deduplicação
canônica por fonte no plano. **997 fontes** têm mecanismo sem data fixa; duas só têm endereços
com data fixa identificados. As leituras gerais de referência são de 17/10; os RSS regionais do G1 foram validados em
18/10 nas rodadas fechadas descritas abaixo.
Os antigos totais 855/944 eram filtros por atualidade e não representam o total funcional.

Exportação offline, finita e sem coleta/agendamento:

```bash
scripts/bm.sh export-mechanisms --out var/mecanismos-busca
```

Arquivos principais:

- `plano-funcional.json`: todos os XML funcionais, compatível com `--discovery-plan`, com resultado
  temporal, data da auditoria, parcialidade e data fixa preservados.
- `hints-funcionais.csv`: todos os mecanismos funcionais para `--hints`.
- `mecanismos.csv`: inventário com `functional` e `fixed_date` separados do resultado temporal.
- `fontes.csv`: todas as fontes únicas, inclusive sem mecanismo funcional.
- `secoes-mecanismos.csv.gz` e `lacunas-secoes.csv.gz`: relações observadas e lacunas por página.
- `resumo.json` e `LEIA-ME.txt`: critérios e contagens.

Os antigos `plano-descoberta.json`/`hints-recentes.csv` e `plano-ampliado.json`/
`hints-complementares.csv` permanecem como seleções opcionais por atualidade. O plano padrão
do projeto e as estratégias do banco não são alterados. Na futura busca manual, use
`discover --discovery-plan var/mecanismos-busca/plano-funcional.json`; orçamento e
`--max-complements` continuam limitando a leitura por rodada. Esse comando faz coleta e não
foi executado nesta entrega. As buscas revalidam HTTP/conteúdo e respeitam robots/escopo.

APIs somente leitura: `/api/mapa/mecanismos?site=CHAVE&mode=functional` (padrão, paginada),
`mode=fixed_date|recent|supplement|review|all` (filtros opcionais) e
`/api/mapa/mecanismos.csv?site=CHAVE&mode=functional` (sem truncar; sem `site`, todo o catálogo).

## Exploração regional limitada: G1 e Folha

Em 18/10/2026, a seleção fechada de páginas já mapeadas identificou estruturas do G1 nas
27 UFs. Derivar um RSS dessas URLs foi uma hipótese, seguida de validação do XML: os
27 endereços responderam como feeds com itens recentes. Cada um listou 100 URLs; entre
93 e 100 pertenciam ao prefixo da UF correspondente. Essa correspondência não comprova
que todas as notícias ou subseções do estado estejam presentes no feed.

Os 27 RSS foram incorporados às auditorias da fonte nacional `g1.globo.com`, preservando
as auditorias existentes e as fontes estaduais já cadastradas. O catálogo não recebeu
27 fontes artificiais novas. Eles aparecem na tela do G1 e no plano funcional.

A Folha apresentou páginas temáticas sobre lugares e dois RSS editoriais interpretáveis
(`cotidiano` e `poder`, vazios nessa leitura), mas não foram confirmadas 27 edições estaduais
independentes. Não se presume a mesma estrutura para todos os veículos.

A rodada teve 31 chamadas à pilha HTTP em 31,7 segundos, com teto de 31 chamadas/120 segundos,
intervalo por host e robots, sem retries, expansão automática ou leitura de artigos.
Evidências: `var/exploracao-regional/rss-estaduais.csv`, `correspondencia-ufs.json`,
`auditorias.json`, `plano-regional.json` e `hints-regionais.csv`. O arquivo
`g1-auditorias-antes.json` preserva a fotografia anterior à incorporação; o procedimento
fechado é `var/exploracao-regional/verificar_rss.py`.

## Conferência encerrada de 20 veículos

Rodada fechada em 18/10/2026: 16 veículos brasileiros e quatro estrangeiros, selecionados
por links de mecanismos declarados ou padrões de RSS/sitemap já válidos em seções equivalentes
do mesmo host. A triagem offline descartou combinações de blogs/hosts/áreas de serviço,
sem fazer chamadas para elas. Não foram testadas combinações indiscriminadas de estados.

A rede terminou em **213,3 segundos**, com **501 requisições físicas**, incluindo robots e
redirecionamentos. Limites: 20 veículos, 30 chamadas por veículo, 900 segundos globais,
quatro workers e intervalo por host. Não houve retries ou nova rodada automática.
Os limites de chamadas, redirects e prazo foram verificados também com transporte simulado.

Resultado: **300 mecanismos funcionais acrescentados em 18 veículos**, incluindo 70 vazios.
**Balobaki Check, Malawi24 e Ukrinform** passaram de nenhuma auditoria funcional para mecanismos
confirmados. Sul21 e Guineenews não acrescentaram mecanismo funcional nesta seleção.
Auditorias válidas anteriores foram preservadas, com backup por veículo antes das inclusões.

Na comparação limitada a até 500 páginas por fonte e cinco exemplos por página, **239 páginas
de seção ganharam correspondência** e houve **719 correspondências de links adicionais**.
Os novos documentos também listaram **12.815 URLs adicionais** sob escopo, após filtro de
formato, em comparação com os documentos antigos armazenados. Isso não representa artigos
lidos, publicação necessariamente recente ou prova de cobertura exclusiva: os retratos têm
datas diferentes. O corpus não foi alterado, e não houve LLM, serviço pago ou agendamento.

Entregas em `var/conferencia-20-veiculos/`: `resultado.xlsx`, `resultado-fontes.csv`,
`mecanismos-validados.csv`, `hints-validados.csv`, `plano-validado.json`, `resumo.json` e
`urls-adicionais.csv.gz`. Os leitores reais de plano/hints, as contagens da planilha e as
regras manuais foram conferidos. A seleção, os procedimentos executados, as auditorias e
backups gzip por fonte permanecem na mesma pasta. O plano funcional global e a tela foram
atualizados; não existe continuação agendada desta conferência.

## Profundidade de expansão de sitemaps

O explorador de descoberta agora segue índices de sitemaps aninhados, com profundidade
padrão máxima de **10 níveis abaixo da entrada** (entrada = nível zero). O parâmetro Python
`max_sitemap_depth` de `explore_site` permite configurar essa profundidade. URLs já visitadas
não são requisitadas novamente durante o caminhamento, inclusive em ciclos.

A profundidade não substitui o orçamento de arquivos: `--max-sitemap-fetches` continua
limitando o total lido (padrão seis). Uma cadeia com entrada mais dez filhos precisa de pelo
menos 11 leituras disponíveis; use orçamento compatível quando for necessário atingir o
nível dez. Os pontos de entrada declarados mantêm prioridade, e a descida dos índices
aninhados não abre uma exploração sem limite. Datas e `lastmod` continuam ordenando o
caminhamento da descoberta; a auditoria de acesso não descarta notícias pela data.

Testes cobrem índices aninhados, ciclo entre índices, parada no nível dez e orçamento
independente de arquivos; a descoberta incremental existente continua com os mesmos
checkpoints e gancho de `lastmod`.

## Auditoria de acesso a notícias pelos sitemaps

Concluída em **2026-10-18**, sem filtro de data de publicação. Universo fechado:
**1.814 URLs de entrada distintas**, com **1.856 associações a 822 fontes** do catálogo.
RSS ficou fora desta rodada. A leitura incluiu índices e sitemaps filhos; os totais abaixo
contam as entradas distintas, sem inflar a contagem pelos filhos percorridos.

| Evidência observada | Sitemaps de entrada | Fontes |
|---|---:|---:|
| Pelo menos uma página de notícia acessível | **1.374** | **656** |
| Também com texto legível na amostra | **1.286** | **623** |

Em **1.742** entradas foi possível interpretar XML. Foram avaliadas **2.165 associações
de amostras**, correspondentes a **1.692 URLs de notícias distintas**. A notícia precisa
retornar HTML, ter título e passar pelas verificações de página de notícia, erro suave,
desafio e redirecionamento. Texto legível exige também aprovação dos critérios de extração;
isso não garante o texto integral. Das fontes com notícia acessível, **33** não tiveram
texto legível confirmado; outras **166** ficaram sem uma página de notícia confirmada.
Falha ou ausência de confirmação na amostra não demonstra impossibilidade de acesso ao site.
O índice principal de O Globo foi confirmado com notícia acessível e texto legível.

A verificação usou profundidade máxima **10** (entrada no nível zero), até **12 XML por
entrada**, **duas tentativas de notícia por associação** e **40 chamadas por entrada**,
respeitando robots.txt. Houve 20 minutos iniciais e uma única conclusão das 189 entradas
que ainda não tinham sido consultadas, com teto adicional de cinco minutos e sem repetir
as entradas concluídas. Resultado: **5.271 chamadas HTTP**, cerca de **22,4 minutos** de
execução, nenhuma entrada pendente de início e nenhum erro interno. A profundidade efetivamente
observada chegou a três; limites e falhas permanecem detalhados por associação no relatório.
Nenhuma notícia foi gravada no corpus e nenhum agendamento foi criado.

Planilha com abas Fontes, Sitemaps, Referências e Critérios:
[`var/auditoria-acesso-sitemaps/resultado.xlsx`](var/auditoria-acesso-sitemaps/resultado.xlsx).
Os CSVs, links de exemplo, avaliações de amostras e resumo JSON estão na mesma pasta.
Este é um retrato amostral de acesso, sem garantia de cobertura integral ou acesso futuro;
não substitui o inventário de mecanismos funcionais, que também inclui RSS.

## Auditoria de acesso a notícias pelos feeds RSS/Atom

Concluída em **2026-10-18**, sem filtro de data de publicação. O inventário contém
**33.949 URLs distintas de feeds**, em **829 fontes** (34.546 associações fonte–feed).
Esta foi uma amostra por fonte: até **três feeds selecionados por fonte**, priorizando
links de exemplo e itens já observados, sem rejeitar feeds antigos, e até **dois links
de notícia por feed**. A verificação encerra a fonte quando confirma texto legível;
portanto, os números abaixo não representam uma auditoria de todos os feeds ou seções.

| Evidência observada | Apenas RSS/Atom | União com sitemaps, sem duplicar fontes |
|---|---:|---:|
| Fontes com página de notícia acessível | **653** | **810** |
| Fontes também com texto legível | **617** | **777** |

O RSS acrescentou **154 fontes** com notícia acessível ao resultado da auditoria de
sitemaps. Em 36 das fontes confirmadas por feed, não houve texto legível confirmado;
outras **176** ficaram sem notícia acessível confirmada nesta amostra. Resultado negativo
amostral não demonstra impossibilidade de acesso ao site ou aos feeds não testados.

Foram consultadas **1.034 URLs distintas de feeds** (1.039 associações fonte–feed),
com **1.148 associações de amostras de notícias**, correspondentes a **1.097 URLs distintas**.
Das URLs de feeds consultadas, **689** levaram a uma notícia acessível e **616** também
forneceram texto legível em pelo menos uma associação. O XML foi interpretado em pelo
menos um feed de **787 fontes**. RSS, Atom e RDF foram aceitos; não se seguiu paginação
nem se buscaram mecanismos novos. As páginas de notícias passaram pelos mesmos critérios
de identificação, erro suave, desafio, redirecionamento, extração e paywall da auditoria
de sitemaps. Texto legível não garante texto integral.

A rodada respeitou robots.txt, intervalo por host, limites de 40 chamadas por fonte,
90 segundos para iniciar novas requisições por fonte e teto global de 20 minutos/15 mil
chamadas. Terminou em cerca de **8 minutos**, com **3.075 chamadas HTTP** e nenhum erro
interno. Todas as 829 fontes tiveram ao menos um feed tentado; duas atingiram limite
local e uma das entradas selecionadas ficou sem tentativa. Esses limites são registrados
no relatório, assim como os feeds não testados por parada antecipada ou limite de três.
Não houve coleta contínua, gravação no corpus, chamadas pagas ou novos agendamentos.

Planilha: [`var/auditoria-acesso-rss/resultado.xlsx`](var/auditoria-acesso-rss/resultado.xlsx).
Abas: **Consolidado** (1.294 fontes únicas do catálogo e evidências RSS/sitemap),
**Fontes RSS**, **Feeds verificados**, **Inventário** (inclui não testados) e **Critérios**.
CSVs, links de exemplo, avaliações de amostras, plano fechado e resumo JSON estão na mesma
pasta. A união combina esta rodada com o retrato anterior dos sitemaps; não constitui
medida de cobertura integral dos sites nem garantia de acesso futuro.

## Fechamento da recuperação estrutural

Retrato consolidado em 2026-10-18T11:44:04.169802+00:00:

| Situação estrutural observada | Fontes |
|---|---:|
| Navegação identificada examinada | 347 |
| Mapa parcial | 441 |
| Acesso restrito | 227 |
| Estrutura não confirmada | 279 |

Foram saneados 26694 palpites sem evidência de seção/navegação que já
haviam sido refutados por 404/410, recurso não HTML ou retorno à entrada. Links de menus e
seções confirmadas não foram descartados por essa regra. A evidência anterior foi preservada
em `var/recuperacao-mapeamento/candidatas-refutadas.jsonl.gz`. Saneamento não é ganho de cobertura.

A seleção congelada tinha 767 fontes. 342 receberam
chamadas; 425 não iniciaram por esgotamento do prazo global.
Houve 1842 chamadas à pilha HTTP e 576 páginas
que passaram de falha/pendência para leitura válida. As novas leituras produziram
202 confirmações de tipo seção, incluindo entradas.
O inventário consolidado tem 27947 registros de seções confirmadas
(excluindo entradas) e 953 fontes com exemplos observados em páginas lidas.

A exploração teve teto de 900 segundos, quatro workers, 20 chamadas e 40 segundos por fonte.
Uma tentativa por URL selecionada, sem retry interno; robots e intervalo por host preservados.
A entrada relida pôde acrescentar até duas leituras de navegação, sem expansão recursiva.
O reconhecimento de menus passou a considerar identificadores CSS explícitos; isso gera
candidatas, não confirmação de tipo. Se o menu atual aponta um palpite refutado, ele é reaberto.

Entrega atual: `var/recuperacao-mapeamento/resumo-fontes.xlsx`,
`var/recuperacao-mapeamento/resultado-final.xlsx` e `var/recuperacao-mapeamento/conclusao.json`.
As planilhas completas anteriores permanecem como fotografias históricas. A tela consulta
o estado atual. A etapa está fechada, sem nova rodada automática, LLM, serviços pagos,
coleta contínua ou gravação no corpus.

## Encerramento anterior do mapeamento estrutural

O escopo desta entrega é o mapa de editorias, subseções e navegação das 1.294 fontes.
A etapa foi encerrada em 2026-10-18T01:58:59.890213+00:00, após auditoria do catálogo e uma rodada final
limitada de exploração. Não há novos lotes ou retomada automática.

| Situação estrutural | Fontes |
|---|---:|
| Navegação identificada examinada | 94 |
| Mapa parcial | 713 |
| Acesso restrito | 227 |
| Estrutura não confirmada | 260 |

A rodada consultou 32 fontes, com 596 chamadas à
pilha HTTP em 550.8 segundos. Aplicou tetos de 100 chamadas e 120 segundos por
fonte, quatro fontes em paralelo e 1.200 segundos globais. As chamadas em andamento terminam
pelos timeouts existentes; exportação e verificação ficam fora do prazo de exploração.
Retentativas e redirecionamentos internos são contabilizados separadamente no transporte.
As restrições conhecidas na entrada foram preservadas. As candidatas da navegação têm
prioridade sobre caminhos inferidos; não houve nova expansão por sitemaps/feeds.

O inventário final contém 673914 registros de páginas e 27774 registros de seções confirmadas por
metadados, 243854 candidatas lidas sem confirmação de tipo e 25030
candidatas pendentes preservadas. Navegação identificada examinada exige entrada válida,
evidência estrutural e ausência de candidatas pendentes/falhas reconhecidas; não comprova
que todas as seções existentes foram descobertas. Falhas, tipos incertos e restrições constam
como lacunas. Relações de descoberta por link não declaram sozinhas hierarquia editorial.

Entrega: `var/mapeamento-final/resumo-fontes.xlsx` traz a visão curta das 1.294 fontes.
`var/mapeamento-final/mapa-estrutural-final.xlsx` contém o detalhamento completo:
inventário completo, seções e candidatas, relações observadas, lacunas finais, artigos/serviços/
taxonomias identificados, possíveis duplicatas e critérios. Cada aba também tem CSV gzip
integral. `var/mapeamento-final/encerramento.json` registra métricas e verificações;
`var/mapeamento-final/RESUMO.md` traz o balanço curto. As evidências e planilhas anteriores
foram preservadas. Os procedimentos utilizados estão em `var/mapeamento-final/procedimentos/`.

Esta etapa tratou somente do mapeamento estrutural, sem descoberta incremental, LLM, serviços
pagos, gravação no corpus ou alteração do cron. Os fluxos de coleta documentados abaixo são
funcionalidades anteriores e não fazem parte desta entrega.

## Fluxo de coleta implementado anteriormente

POC de um sistema separado que varre as 1294 fontes de notícias confiáveis da Mídias IA e alimenta
o corpus (Postgres) e o armazenamento de evidências. A meta é aposentar o ramo de busca na web do
pipeline de Mídias IA. Corpus **por entidade, não por adversidade**: entra toda matéria que nomeia
uma pessoa ou organização identificável (a categoria "Diversos" do KyX recebe a neutra).

```
buscar notícias novas nos sites ──► analisar o contexto ──► conferir o critério ──►
capturar a evidência ──► gravar no corpus
```

Um único comando faz tudo, em ordem, e cada etapa é retomável pelo estado (o que falhar fica
pendente para a próxima execução):

```bash
scripts/run.sh --only-file data/poc_sample.txt     # = python -m buscador_midias run ...
scripts/install_cron.sh                            # instala 06:00, 12:00 e 16:00 no cron
```

| Passo | Etapa | Comando avulso |
|---|---|---|
| 0. Revisar estratégias | reavalia só as fontes na vez (veja "Reavaliação automática") | `probe-sources --due` |
| 1. Buscar notícias novas | estratégia da fonte → sitemap → feed → sitemap pelo Bright Data | `discover` |
| 2. Analisar o contexto | texto por HTTP direto + LLM: entidades, temas, fato adverso | `enrich` |
| 3. Conferir o critério | `criteria.py`: matéria **lida** e com **entidade** identificável | (dentro do `enrich`) |
| 4. Capturar a evidência | só das que atendem ao critério | `enrich` |
| 5. Gravar no corpus | Postgres `pgsql_midias`, idempotente | `corpus-sync` |

Não há mais triagem por manchete: ela descartava ~2% das URLs, custava mais do que poupava e a
decisão de verdade é a análise do texto. O único filtro antes da análise é o barato, pela forma da
URL (listagem, seção, mídia).

Veja o plano em [DESIGN.md](DESIGN.md).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Qualidade

```bash
.venv/bin/pytest          # a suíte nunca acessa a internet
.venv/bin/ruff check .
.venv/bin/mypy
```

## Sondagem de viabilidade (faz requisições reais!)

Para cada fonte lê o `robots.txt` e os sitemaps (no máximo 6 por site) e relata: tem sitemap?
é índice? é news sitemap? tem `lastmod`? quantas URLs nas últimas 24h/7d? bloqueia?

```bash
.venv/bin/python -m buscador_midias probe --limit 5            # teste pequeno
.venv/bin/python -m buscador_midias probe                      # as 1294 fontes
# opções: --sites CSV  --out CSV  --workers 8  --interval 1.0 (s entre requisições ao mesmo host)
#         --only CHAVE [CHAVE ...]   (ex.: g1.globo.com/globonews; chave desconhecida = erro)
#         --progress-every 50        (progresso em stderr a cada N fontes)
```

### Fallback com Bright Data (opt-in, consome créditos)

Sites que bloqueiam o nosso agente (HTTP 401/403/429, ou HTML com status 200 no lugar de XML)
podem ser buscados pelo **Web Unlocker** do Bright Data. Mesmo contrato da captura de evidência
do KyX: `POST {BASE_URL}/request` com `{zone, url, format: "raw"}`.

```bash
export BRIGHTDATA_API_KEY=...            # nunca vai para log nem para mensagem de erro
export BRIGHTDATA_ZONE_UNLOCKER=...
# BRIGHTDATA_BASE_URL é opcional (padrão https://api.brightdata.com)

.venv/bin/python -m buscador_midias probe --bright-data --bright-data-max-requests 40 \
    --only maisgoias.com.br observer.ug 38north.org
```

- Só usa o fallback para hosts que **já nos bloquearam**; o primeiro bloqueio marca o host e as
  demais requisições dele vão direto ao Unlocker (sem insistir no acesso gratuito).
- `--bright-data-max-requests` (padrão 500) é o teto de custo da execução; esgotado, o resultado
  volta a ser `blocked`. O resumo imprime `bright_data_requests` e `bright_data_hosts`.
- O `robots.txt` continua sendo respeitado: `robots_disallowed` nunca vai ao Unlocker.

Falhas transitórias (timeout, HTTP 502/503/504) são repetidas uma vez com espera. Um índice lido
cujos filhos falham é classificado como `blocked`/`error`, não como `ok`.

O relatório vai para `var/probe-report.csv` (ignorado pelo git). Respeita o `robots.txt`.

## Descoberta incremental

### Antes da coleta: inventário estrutural retomável

O objetivo atual é mapear as fontes e suas seções antes de ampliar a coleta. As planilhas
existentes em `var/` são os resultados anteriores; não precisam ser substituídas ou descartadas.
`map-sites` inicia um inventário separado, mantendo a estratégia anterior como referência no
relatório. Os nomes de seção truncados das planilhas antigas não viram URLs por adivinhação.

```bash
# Aplicar antes os DDLs em sql/postgres/, nesta ordem:
# 004_create_midias_crawler_section_pages.sql
# 005_create_midias_crawler_section_sources.sql
# 006_add_midias_crawler_page_classification.sql
scripts/bm.sh map-sites --only folha.uol.com.br g1.globo.com m28investigates.com bbs.bt/news \
    --pages-per-site 10 --out var/mapa-sites-piloto.xlsx
# Repetir o comando continua pela fila, sem reler as páginas concluídas.
scripts/bm.sh map-sites --report-only --out var/mapa-sites.xlsx  # catálogo inteiro, sem rede
# Relatório curto para priorizar lacunas (somente leitura, sem HTTP):
scripts/bm.sh map-sites --report-only --priorities-only --out var/mapa-prioridades.xlsx
scripts/bm.sh map-sites --retry-failed --only folha.uol.com.br  # nova tentativa das falhas
# Atualiza a amostra da página e confere mecanismos (DDL 005 aplicado):
scripts/bm.sh map-sites --only g1.globo.com --refresh-pages --pages-per-site 12 \
    --validate-sources --source-requests 20 --out var/mapa-sites-piloto.xlsx
scripts/bm.sh map-sites --only g1.globo.com --sources-only --source-requests 20
# Complementação dos mecanismos nas seções já mapeadas, com seleção limitada:
scripts/bm.sh map-sites --drain --sources-only \
    --only-file var/mapa-complementacao-piloto.txt \
    --source-requests 20 --max-site-requests 200 --max-site-seconds 180 \
    --max-run-seconds 1800 --workers 4 --out var/mapa-complementacao.xlsx
```

Cada página processada grava um checkpoint atômico com as novas pendências no Postgres. Home e
candidatas a seção são exploradas seguindo novas seções. A entrada vem primeiro, depois as
candidatas apontadas explicitamente na navegação e, por último, os palpites pelo formato da URL;
dentro de cada grupo, páginas mais rasas primeiro. Guarda endereço, primeira
página ou mecanismo que o apontou, título, links com aparência de matéria, feeds/sitemaps apontados, índices
de feeds, tentativas e falhas. Tags, autores e diretórios de tópicos (`topic`, `topics`,
`topics-list`, `themen`, `tagg`, `amnen`, `persons`, `mot-cle`, `mots-cles`, `tema`, `topico`)
ficam como
taxonomias excluídas para revisão.
Essa classificação também é
aplicada às filas antigas ao retomá-las, preservando os registros e as amostras já salvas.
Uma URL de entrada cadastrada nesses caminhos continua sendo lida.
Rotas de serviço observadas também ficam excluídas: o guia de TV em `tvguide.vg.no`,
a previsão em `tempo.folha.uol.com.br`, os diretórios de previsão da Euronews
(`weather`, `idojaras`, `meteo`, `wetter`, `hava-durumu`, `tempo`, `meteorologia`, `tiempo`),
`lvbp/estadisticas` do El Nacional, contatos de perfis `/<perfil>/contacts` em
`blogs.mediapart.fr` e caminhos de atendimento do Correio do Estado.
Gráficos e históricos de preços sob `/nkd/company/us/<ticker>/chart|history` da Nikkei,
e os diretórios de citações e biografias `citations`/`celebre/biographie` da Evene/Le Figaro,
também são serviços. Perfis de empresas e notícias culturais continuam elegíveis.
O diretório `noticias-assinantes` continua elegível; restringir acesso não o torna
automaticamente uma rota de serviço. Uma regra de serviço retirada reabre as candidatas
antigas a seção, mantendo ciclos excluídos e sem enfileirar matérias pelo título longo da URL.
Essas regras são limitadas aos hosts e caminhos identificados; uma editoria de clima
ou uma notícia sobre atendimento em outro caminho continua elegível. Na retomada,
registros antigos são reclassificados preservando títulos, amostras e datas.
Rotas com três segmentos iguais consecutivos (`category/category/category`, `index/index/index`)
são preservadas como possível ciclo de navegação,
sem continuar expandindo os links relativos. Seções profundas sem esse padrão continuam elegíveis.
O limite de páginas vale **por lote** (inclui páginas proibidas; robots é uma requisição extra).
A execução usa somente HTTP gratuito, respeita robots e intervalo por host; não chama LLM,
Bright Data, enriquecimento ou escrita no corpus.

Com **`--drain`**, `--pages-per-site` é o tamanho de cada lote interno. Há também limites totais
por execução: `--max-site-requests` (padrão 200 chamadas à pilha HTTP por fonte),
`--max-site-seconds` (180 segundos por fonte) e `--max-run-seconds` (1800 segundos de exploração
para o conjunto, incluindo a espera das fontes na fila de workers). O saldo da fonte é
compartilhado entre páginas, robots, mecanismos e todos os lotes; redirecionamentos e
retentativas internos da pilha não são contados separadamente. Os prazos são conferidos antes
de cada nova chamada: uma chamada em andamento termina sob os timeouts HTTP existentes.
A exportação fica fora desse prazo. Atingir o limite mantém as pendências e registra o motivo
na entrada, sem marcar páginas como falhas de rede ou declarar a fonte concluída.
A fonte também para ao esgotar as pendências reconhecidas ou deixar de avançar.
Falhas e releituras só entram no primeiro lote interno. As fontes são independentes:
uma falha não interrompe as demais. O modo contínuo não mantém em memória o cache HTTP dos hosts
compartilhados e atualiza o relógio entre lotes. Na auditoria, continua pelos mecanismos ainda não
tentados nesta execução e pelos novos feeds revelados por índices HTML; `--source-requests` vale
por passagem. Uma leitura interrompida por orçamento permanece parcial e não é repetida
indefinidamente. Esgotar a fila reconhecida não comprova cobertura de seções só em JavaScript.
Depois da auditoria, o modo contínuo deriva candidatas dos prefixos de URLs listadas em
feeds/sitemaps lidos (`mechanism_path`) e volta à exploração se houver caminhos novos. Mantém o
escopo da fonte, não reabre páginas conhecidas e exclui datas, taxonomias e rotas de serviço.
Um prefixo derivado continua `unknown` até ser lido: não é uma seção confirmada. A aba Fontes
mostra essa origem separadamente; navegação explícita pode promovê-la na prioridade da fila.
O inventário confere robots por caminho: uma entrada proibida não impede a leitura de uma
subseção explicitamente permitida. Falha ao baixar a política mantém a fila daquele host
suspensa; outros hosts continuam sujeitos às suas próprias políticas.

Três falhas consecutivas de conexão na mesma origem (DNS, TLS, timeout ou transporte),
dentro do mesmo lote de páginas ou passagem de auditoria,
suspendem somente aquela origem e preservam suas páginas ainda não tentadas. Os demais hosts
da fonte continuam sendo explorados, incluindo subseções novas apontadas pelos mecanismos.
O motivo fica na entrada; uma nova execução pode retomar a origem suspensa. A auditoria mantém
sua própria suspensão por origem, sem reiniciá-la entre passagens da mesma execução.
Respostas 404 e proibições de caminho não
disparam essa suspensão. Falha de conexão não é tratada como seção sem notícias.

O relatório contém **Fontes**, **Páginas**, **Mecanismos**, **Amostras**, **Cobertura**, **Lacunas**,
**Complementos** e **Revisão**,
sem corte nas 40 maiores
seções. Mecanismos inclui também todos os candidatos já sondados no banco (a base das planilhas
anteriores), com o veredito anterior e sem inventar uma associação a uma seção.
CSV gera um arquivo por aba. `--report-only` abre sessão somente leitura e inclui todas
as fontes selecionadas, mesmo as ainda não iniciadas.
Se uma aba exceder o limite de linhas do Excel, o XLSX continua em abas numeradas (`Amostras 2`,
por exemplo), repetindo o cabeçalho sem descartar registros.

**A exploração identifica candidatas, não comprova cobertura.** A classificação de seção
ainda é heurística pela URL; não reconhece toda navegação (por exemplo seções só em JavaScript,
URLs com query ou caminhos fora dos padrões atuais). Fila explorada significa apenas que os
links reconhecidos acabaram. Nada disso liga `map-sites` ao cron.

**Conferência de amostras:** `--validate-sources` mede os feeds/sitemaps apontados nas páginas,
os candidatos úteis já sondados e as dicas existentes; `--sources-only` faz só essa etapa. O teto
`--source-requests` conta leituras pela pilha HTTP, por fonte, incluindo robots de cada origem e
filhos de índices; retentativas e redirecionamentos internos da pilha ficam à parte. As exclusões
de `estrategias_manuais.csv` continuam valendo. Leituras ficam em
`midias_crawler_section_sources` (DDL `005_create_midias_crawler_section_sources.sql`), com todas
as URLs listadas, veredito, data, custo em leituras e indicação de leitura parcial. Há
checkpoint por mecanismo. Endereços não tentados ficam para o lote seguinte; os já conferidos
só entram novamente se a amostra da página for mais nova ou após 24 h. Leituras interrompidas
por orçamento têm prioridade de retomada; `--retry-failed` também vale para falhas dos mecanismos.
Páginas HTML que apontam feeds recebem `feed_index`: seus links alimentam a fila persistida,
mas só a leitura dos feeds/sitemaps de destino pode comprovar correspondência com matérias.
Links desses índices para feeds de taxonomias são preservados, mas não disparam uma varredura
de todas as palavras-chave ou autores. Continuam elegíveis quando uma página do inventário
os declara ou quando constam das dicas, regras forçadas ou candidatos anteriores úteis.
Leituras já feitas continuam disponíveis para comparar as amostras das seções.
Paginação HTML em `/index/feed/pagina-N` recebe `html_pagination`: o nome do caminho não
a transforma em feed, nem dispara uma varredura do arquivo histórico. Índices desse formato
registrados antes da correção deixam de expandir a fila. XML válido continua sendo medido.
Feeds cujos links apontam exclusivamente a comentários (`#comment-N` ou `replytocom`) recebem
`comments_feed`, preservando as observações e sem comprovar cobertura de notícias. Em feeds
mistos esses links também não entram na comparação canônica. A palavra `feed` no meio de um
título de matéria não basta para descobrir um mecanismo.
Isso também vale para matérias sob uma editoria `/atom` e hosts resolvidos indevidamente
como caminhos relativos sob `/feed`. Uma resposta HTML nesse formato recebe `html_article`
e não expande a auditoria. Índices antigos são corrigidos preservando URLs, datas e custos.
Metadados de artigo da própria página também interrompem essa expansão em HTML.
Endpoints que terminam em `/feed`, índices HTML de feeds e feeds declarados continuam
elegíveis; uma resposta XML válida prevalece sobre a aparência de matéria da URL.

O relatório compara URLs canônicas e mostra, por página, quantos links da amostra aparecem nos
mecanismos medidos e quais ficaram de fora. `sample_full` é **amostra coberta**, `partial` é
correspondência parcial, `no_match` é nenhuma correspondência; `pending`, `not_read`, `no_sample`
e `source_failed` distinguem falta de leitura, de amostra ou falhas dos mecanismos. Uma leitura
anterior à página não comprova a amostra atual. Esses estados não afirmam cobertura total do site.

A aba **Complementos** sugere mecanismos por seção pelo ganho observado: primeiro aquele que
encontra mais links da amostra, depois os que acrescentam links ainda não encontrados. Em empate,
prefere leitura não parcial e depois ordena pela URL. Mecanismos redundantes continuam na aba
Cobertura, mas não recebem indicação de complemento. A sugestão inclui datas, quantidade de
links correspondentes, ganho incremental e aviso de leitura parcial; não garante o menor conjunto
possível, cobertura futura ou cobertura de links não observados. As lacunas continuam explícitas.
Essa indicação é só um relatório: ainda não configura a coleta ou o cron.

**Prioridades de cobertura:** `--report-only --priorities-only` exporta uma linha por fonte,
sem HTTP nem alterações no banco. Prioridade 1: há leitura válida, mas nenhum mecanismo
recente confirmado; prioridade 2: existem mecanismos recentes, mas faltam correspondências
ou associação por seção; prioridade 3: entrada sem leitura válida ou bloqueada, para revisão;
prioridade 4: as amostras disponíveis têm correspondência completa. Seções sem amostra e
pendências continuam explícitas mesmo na prioridade 4. Um mecanismo recente precisa ser
RSS/Atom ou sitemap lido nas últimas 24 horas, com veredito `fresh`/`slow` e URLs no escopo
original da fonte. Feeds sem datas são contados separadamente, para conferência.
As quantidades de links somam ocorrências nas amostras das páginas; não são notícias únicas.
O relatório orienta complementações com orçamento definido, sem alterar estratégias ou
disparar varreduras. A coleta deve ser validada depois em um piloto.

A aba **Revisão** mostra as entradas com falha ou observação, mantendo estado, motivo e quantidade
de páginas lidas na fonte. Distingue proibição explícita de robots de falha ao baixar o arquivo:
HTTP 403 em `robots.txt` não comprova uma regra de proibição, mas mantém as páginas suspensas.
Também distingue limite de requisições, DNS transitório ou domínio a revisar, TLS, rede/HTTP
transitório, acesso bloqueado, mudança de escopo e conteúdo inválido. **Falha transitória** indica
apenas uma sugestão de tentativa limitada; não dispara retentativas nem altera catálogo, política
robots, validação TLS ou intervalos. As falhas de subseções continuam na aba Páginas.

Links de matérias em `<nav>`, `<footer>`, `<aside>` e elementos com `role="navigation"` não entram
na amostra; quando há `<main>`, a amostra
fica nesse conteúdo. `--refresh-pages` relê páginas concluídas para atualizar a amostra. As seções
encontradas na navegação continuam entrando no inventário. Ainda falta ampliar o reconhecimento
de navegação, resolver mecanismos para as lacunas e conferir seções com conteúdo só em JavaScript.

**Primeiro lote do catálogo (2026-10-17):** as 1.294 fontes tiveram a entrada avaliada; 1.001
tinham ao menos uma página lida com sucesso e 293 ficaram sem leitura válida. O inventário
registrou 36.003 candidatas a seção/subseção e 36.049 páginas pendentes (incluindo entradas cuja
leitura falhou antes de abrir a página). Isso não significa que as seções foram confirmadas ou
que o mapeamento terminou. Resultado consolidado: `var/mapa-sites.xlsx`.
No piloto (`var/mapa-sites-piloto.xlsx`), 35 mecanismos conferidos e 113 páginas lidas: 10 amostras
com correspondência completa, 53 parcial, 39 sem correspondência nos documentos medidos e 11
páginas sem amostra reconhecida. Os resultados são uma fotografia desse lote, não metas de cobertura.

**Evidência do tipo de página (DDL 006):** a aba Páginas distingue `Tipo` (como o endereço entrou
no inventário) de `Tipo identificado`, `Evidência do tipo` e `Como foi encontrada`. A classificação
reconhece `CollectionPage` da própria URL como seção; um schema de artigo da própria URL com
manchete e data como matéria; rotas inequívocas e schemas de serviço como serviço. Um artigo
destacado com outra URL não muda o tipo da seção que o contém. `og:type=article` sozinho não
basta; schema sem identidade só é aceito com manchete, data, og:type e H1 concordantes.
Conflitos mantêm `unknown`, para revisão. Isso identifica o tipo, não a cobertura das notícias.

Matérias e serviços ficam registrados, com motivo, mas não expandem a fila de seções nem entram
nas métricas de cobertura por seção. As regras não apagam o histórico. Endereços como
`/politica/index.html` passam a ser candidatos quando a navegação explícita os aponta.
O reconhecimento continua heurístico; páginas antigas sem evidência permanecem `unknown` até
nova leitura com `--refresh-pages`. Aplicar `sql/postgres/006_add_midias_crawler_page_classification.sql`
antes de usar esta versão do mapeamento. As demais etapas não exigem esse DDL.

**Segundo lote (2026-10-17):** até 3 páginas pendentes por fonte em 935 fontes com leitura válida,
seguido de conferência em 20 fontes conhecidas. Acumulado: 3.793 páginas lidas, 44.520 candidatas
a seção e 41.759 páginas pendentes. A fila cresceu porque as páginas abertas revelaram novas
candidatas. A conferência das 20 fontes registrou 1.005 candidatas apontadas na navegação,
18 páginas declaradas como seções nos metadados e 24 páginas de serviço. Relatório:
`var/mapa-sites-validacao20.xlsx`; consolidado: `var/mapa-sites.xlsx`. A lista das 904 fontes
selecionadas para a rodada seguinte foi preservada em `var/mapa-secoes-lote3-fontes.txt`.

**Terceiro lote (2026-10-17):** até 5 páginas pendentes por fonte nas 904 fontes selecionadas,
com resultado registrado para todas. Acumulado: 7.837 páginas lidas, 65.298 candidatas a seção e
58.321 páginas pendentes. A classificação identificou 764 seções por metadados, 182 matérias e
1.201 páginas de serviço; 20.013 candidatas foram apontadas na navegação. Esses números não
comprovam cobertura total e a fila continua crescendo com as novas subseções encontradas.

O consolidado tem 153 mecanismos conferidos: 45 amostras com correspondência completa, 119
parciais e 72 sem correspondência nos documentos medidos. A aba Complementos registra 48
indicações que acrescentam links além do primeiro mecanismo sugerido para cada seção.
Os relatórios estão em `var/mapa-sites.xlsx`, `var/mapa-sites-lote3.xlsx` e
`var/mapa-sites-validacao20.xlsx`. `var/mapa-resumo.json` guarda os totais dessa fotografia.
`var/mapa-secoes-proximo.txt` foi atualizado com 860 fontes com leitura válida e fila aberta.
`var/mapa-revisao-entradas.csv` separa 300 entradas que precisam de revisão: 137 casos ligados
a robots, 94 bloqueios, 22 redirecionamentos, 13 falhas de DNS e 34 outras falhas de leitura.
Algumas dessas fontes têm outras páginas lidas; o CSV informa essa contagem e preserva o motivo.

**Quarto lote (2026-10-17):** até 3 páginas pendentes nas 860 fontes selecionadas, com resultado
registrado para todas. Uma retentativa independente em 24 fontes com falhas transitórias obteve
leitura inicial em `the-star.co.ke`, `rtnc.cd` e `syriadirect.org`; essas três fontes receberam
uma etapa adicional de exploração e conferência de mecanismos. RTNC voltou a apresentar falha
de DNS durante a expansão, mantida no relatório. Seleções preservadas em
`var/mapa-secoes-lote4-fontes.txt`, `var/mapa-retentativas-lote4.txt` e
`var/mapa-recuperadas-lote4.txt`.

Também foram conferidos 137 mecanismos em 20 fontes adicionais de diferentes países, após
concluírem a exploração desta rodada: 38 amostras com correspondência completa, 103 parciais e
26 sem correspondência nos documentos lidos. Seleção:
`var/mapa-conferencia-novas-lote4.txt`; relatório:
`var/mapa-sites-conferencia-novas-lote4.xlsx`. A fotografia anterior do consolidado foi preservada
em `var/mapa-sites-antes-lote4.xlsx` para comparação.

Acumulado após o quarto lote: 10.164 páginas lidas, 73.360 candidatas a seção e 63.958 páginas
pendentes. São 1.274 seções confirmadas por metadados e 21.607 candidatas apontadas na navegação.
Os 301 mecanismos conferidos encontram completamente 87 amostras e parcialmente 228; 100 amostras
não têm correspondência nos documentos lidos. A aba Complementos registra 198 indicações que
acrescentam links além do primeiro mecanismo sugerido por seção.
`var/mapa-sites.xlsx` e `var/mapa-resumo.json` são o consolidado desta fotografia.
`var/mapa-secoes-proximo.txt` contém 832 fontes com leitura válida e fila aberta.
`var/mapa-revisao-entradas.csv` agora é uma cópia da aba Revisão, com 299 entradas: 22 proibições
explícitas de robots, 112 leituras bloqueadas do robots e os demais motivos separados, preservando
o texto original. Há 25 entradas com indicação de falha transitória; as que estão fora da fila
principal ficam em `var/mapa-retentativas-proximo.txt`, sem repetir a seleção principal.

**Quinto lote (2026-10-17):** exploração de até 4 páginas por fonte nas 832 selecionadas e uma
retentativa independente nas 18 fontes com falhas transitórias. Essas retentativas iniciais não
obtiveram nova leitura de entrada. Após um diagnóstico de DNS, uma tentativa direcionada em
`jornalnoticias.co.mz` obteve 3 páginas lidas, 20 candidatas a seção e 5 mecanismos conferidos.
Relatório da fonte recuperada: `var/mapa-sites-jornalnoticias-lote5.xlsx`.

O diagnóstico da fotografia anterior da fila encontrou 118 variações potenciais de esquema ou
`www` entre 63.958 pendências. Não encontrou caminhos fortemente repetitivos pela heurística
registrada em `var/mapa-fila-diagnostico-lote5.json`. As variações não foram tratadas como
equivalências confirmadas nem descartadas do inventário.

Foram conferidos 289 mecanismos em 40 fontes adicionais, sendo 20 brasileiras e 20 internacionais
que tiveram novas páginas lidas nesta rodada: 75 amostras com correspondência completa, 229
parciais e 137 sem correspondência nos documentos lidos. Seleção e relatório conjunto:
`var/mapa-conferencia-novas-lote5.txt` e `var/mapa-sites-conferencia-novas-lote5.xlsx`.
As listas da exploração e das retentativas estão em `var/mapa-secoes-lote5-fontes.txt` e
`var/mapa-retentativas-lote5.txt`. A fotografia anterior foi preservada em
`var/mapa-sites-antes-lote5.xlsx`.

Todas as 832 fontes do lote tiveram resultado registrado. Acumulado após a rodada: 13.130 páginas
lidas, 79.734 candidatas a seção e 67.236 páginas pendentes; 1.944 seções foram confirmadas por
metadados e 23.132 candidatas foram apontadas na navegação. As 1.294 fontes continuam no
consolidado, das quais 1.004 têm ao menos uma página lida com sucesso.
Os 595 mecanismos conferidos encontram completamente 164 amostras e parcialmente 458; 237
amostras não têm correspondência nos documentos lidos. A aba Complementos registra 374
indicações que acrescentam links além do primeiro mecanismo sugerido por seção.
Naquela fotografia, a fila de continuidade tinha 791 fontes com leitura válida e pendências
e havia 299 entradas para revisão. Os arquivos principais são atualizados pela consolidação
mais recente; as planilhas com sufixo de lote preservam os resultados anteriores.

**Encerramento da varredura aberta (2026-10-17):** os quatro processos foram interrompidos
por decisão do usuário para consolidar checkpoints e priorizar lacunas de cobertura.
Nenhuma nova varredura geral ou complementação foi iniciada. A normalização final preservou
amostras, títulos e datas ao atualizar 21 classificações antigas de taxonomias/serviços.
O inventário detalhado fica em `var/mapa-sites.xlsx`; a visão curta para orientar o próximo
trabalho fica em `var/mapa-prioridades.xlsx` e `var/mapa-prioridades.csv`.
`var/mapa-encerramento.json` registra o motivo e os hashes das filas estruturais preservadas;
`var/mapa-diagnostico-final.json` distingue essas filas das origens suspensas por conexão/robots.
`var/mapa-resumo.json` guarda os totais da consolidação. As listas de seções, mecanismos e
retentativas representam pendências disponíveis para revisão, sem execução automática.
`var/mapa-complementacao-piloto.txt` propõe até 20 fontes de prioridade 1 com amostra existente,
ordenadas pelo número de ocorrências sem correspondência. A execução posterior desse piloto
está descrita abaixo; essa lista preserva a seleção inicial.
A planilha parcial `var/mapa-sites-parcial-atual.xlsx` permanece como fotografia anterior.

Fotografia consolidada desse encerramento: 1.294 fontes, 1.014 com leitura válida,
272.161 páginas lidas, 27.711 páginas identificadas como seção por metadados e
64.286 mecanismos conferidos. Permanecem 25.256 páginas pendentes em 142 fontes;
isso inclui filas preservadas para consolidação e origens suspensas por acesso/conexão.
Das amostras, 23.211 têm correspondência completa, 58.647 parcial e 85.895 não têm
correspondência nos mecanismos medidos. Há 30.559 sugestões incrementais além do primeiro
mecanismo por seção. Os totais de páginas e amostras são registros por fonte/URL, não notícias
únicas nem comprovação de cobertura total. O XLSX detalhado usa 17 abas com continuações
numeradas para respeitar o limite de linhas do Excel.

O relatório curto separa 167 fontes na prioridade 1, 803 na prioridade 2, 280 na prioridade 3
(223 com acesso bloqueado e 57 sem leitura estrutural válida) e 44 na prioridade 4.

**Piloto de complementação e descoberta (2026-10-17):** as 20 fontes selecionadas foram
conferidas com teto de 60 chamadas à pilha HTTP/90 segundos por fonte e 600 segundos globais.
Como 17 não tinham fila automática de mecanismos, a conferência também usou mecanismos
observados, sitemaps declarados no robots e caminhos usuais de RSS/sitemap, respeitando as
exclusões manuais. Foram 523 chamadas em 257 segundos, com 302 leituras/reavaliações e sem
erros de processamento. Houve ganho em 15 fontes: 16.508 URLs distintas das amostras passaram
a ter correspondência, sem perda de correspondências anteriores. As 658.121 ocorrências
adicionais repetem URLs entre páginas; não representam essa quantidade de notícias únicas.
Foram encontrados 42 mecanismos novos com conteúdo recente e exportados 68 endereços recentes
reutilizáveis, incluindo mecanismos reavaliados.

A comparação usou as mesmas páginas/amostras congeladas, com verificação de integridade
antes/depois. As 25.256 pendências estruturais ficaram preservadas. As prioridades globais
passaram a 152 fontes na prioridade 1, 818 na prioridade 2, 280 na prioridade 3 e 44 na 4.
As cinco fontes do piloto ainda sem mecanismo recente confirmado são `elpais.com`,
`chinadaily.com.cn`, `chinafile.com`, `rtc.cv` e `caixin.com`; os motivos estão em
`var/piloto-revisao-pendencias.csv`.

A descoberta foi validada separadamente em Times of India, Respekt, La Stampa, Jeune Afrique
e Mediapart, com os parsers de produção, união por hash canônico e duas leituras.
O controle com índices amplos encontrou 62.728 candidatas na primeira leitura, 21.660 além
do mecanismo principal do piloto; 79 dessas adicionais tinham publicação declarada nas
últimas 48 horas. Sua segunda leitura em Times of India ficou parcial ao atingir o orçamento.
A checagem priorizando sitemaps de notícias e feeds datados teve duas leituras concluídas
nas cinco fontes, 60 chamadas e 6.046 candidatas na primeira leitura: 2.418 adicionais,
das quais 345 com publicação declarada nas últimas 48 horas. Os limites dessa checagem foram
20 chamadas/60 segundos por fonte e 180 segundos globais. Os dois testes têm seleções e
orçamentos distintos e não estimam a cobertura total dos sites.

O resultado reforça a seleção por recência, publicação declarada e ganho incremental, além
da correspondência das amostras. `lastmod` de sitemap não comprova publicação; as URLs ainda
são candidatas pelo filtro L0, sem leitura dos artigos ou análise de entidades.
O piloto não alterou estratégias, estado incremental, corpus ou cron e não usou LLM nem
serviços pagos. A união validada agora faz parte do fluxo automático de descoberta, conforme
as regras e os limites abaixo.

Arquivos deste piloto:

- `var/piloto-resultados.xlsx`: visão curta com complementação, descoberta, prioridades das
  1.294 fontes e mecanismos reutilizáveis.
- `var/piloto-validacao-final.json`: métricas, verificações e limitações dos testes.
- `var/mapa-prioridades-pos-piloto.xlsx` e `.csv`: prioridades globais atualizadas.
- `var/piloto-complementacao-resultado.xlsx`: ganho por seção e comparação antes/depois.
- `var/piloto-descoberta-resultado.xlsx` e `var/piloto-descoberta-recente-resultado.xlsx`:
  resultados e URLs das duas seleções de descoberta.
- `var/piloto-mecanismos-validados.csv`: dicas compatíveis com `--hints`, sem alterar os CSVs
  manuais; a validade de um endpoint continua sujeita a nova conferência.
- `var/piloto-proximas-fontes.txt`: próxima seleção de até 20 fontes ainda na prioridade 1,
  excluindo as já tentadas no primeiro piloto; esta seleção foi executada no lote 02 abaixo.

`var/mapa-sites.xlsx`, `var/mapa-resumo.json` e `var/mapa-prioridades.xlsx` continuam sendo a
fotografia histórica do encerramento anterior. As auditorias novas estão no banco e nos
arquivos específicos deste piloto; a planilha detalhada de 556 MB não foi regenerada.

**Segundo lote dirigido (2026-10-17):** as 20 fontes de `var/piloto-proximas-fontes.txt`
foram conferidas em 125,7 segundos, com 314 chamadas à pilha HTTP, sob os mesmos tetos de
60 chamadas/90 segundos por fonte e 600 segundos globais. Foram 231 leituras/reavaliações,
213 mecanismos novos e 31 mecanismos novos classificados como recentes. Sete fontes passaram
a ter mecanismo recente confirmado; foram exportados 42 endereços reutilizáveis dessas fontes.
Oito fontes tiveram ganho nas mesmas amostras congeladas: 2.288 URLs únicas por fonte ganharam
correspondência (4.463 ocorrências entre páginas), sem perda das correspondências anteriores.
ColombiaCheck teve ganho amostral, mas continuou sem mecanismo recente confirmado: ganho de
correspondência e recência são medidas diferentes. Treze fontes seguem para revisão. As prioridades globais ficaram em 145/825/280/44
(níveis 1 a 4), preservando as limitações das amostras.

Resultados em `var/lote-02/resultados.xlsx`, com resumo das 20 fontes, leituras dos mecanismos,
pendências, mecanismos reutilizáveis e prioridades globais. `var/lote-02/validacao-final.json`
registra verificações e limitações; `RESUMO.md` explica a rodada e `pendencias.csv` traz os
motivos observados. Os CSVs e snapshots do primeiro piloto foram preservados. A lista
`var/lote-02/proximas-fontes.txt` prepara outro lote, que não foi executado.

Este lote atualizou apenas auditorias do inventário. Não explorou novas seções nem alterou
estratégias automáticas, estado incremental, corpus ou cron. Não usou LLM ou serviços pagos.
Os mecanismos exportados são dicas para posterior validação incremental; não foram promovidos
ao plano de descoberta. A correspondência amostral não comprova cobertura total, e `lastmod`
não comprova publicação.

Exemplo de reavaliação limitada dos mecanismos encontrados, depois da janela de cache ou
com novas amostras (não inicia pesquisa automática de endpoints ausentes):

```bash
scripts/bm.sh map-sites --sources-only --drain \
    --only-file var/mapa-complementacao-piloto.txt \
    --hints data/discovery_hints.csv data/hints_manuais.csv var/piloto-mecanismos-validados.csv \
    --source-requests 20 --max-site-requests 60 --max-site-seconds 90 \
    --max-run-seconds 600 --workers 4 --out var/mapa-reavaliacao-piloto.xlsx
```

Cada rodada devolve só o que ainda não vimos. O estado vive no **Postgres** (tabelas `midias_crawler_*`, veja "Estado" abaixo).

```bash
.venv/bin/python -m buscador_midias discover --only-file data/poc_sample.txt \
    --bright-data --bright-data-max-requests 60 --corpus
.venv/bin/python -m buscador_midias report
```

### Cascata de fontes: sitemap, feed e Bright Data

Para cada site com complementos escolhidos, a descoberta tenta o principal (ou a reserva se
o principal falhar) e lê os complementos selecionados. Une as URLs pelo hash canônico e guarda
`site_runs.source=combined` quando mais de um mecanismo contribui. Se nenhum mecanismo do
conjunto servir, segue a cascata abaixo; nessa cascata, a primeira fonte que traz URLs encerra
a tentativa. Sem complementos, preserva a tentativa sequencial do principal e da reserva:

1. **Sitemap gratuito** (`sitemap`).
2. **Feed RSS/Atom gratuito** (`feed`): os feeds que a home declara (até 3) ou, sem declaração,
   `/feed/`, `/rss.xml` e `/rss`. Respeita o `robots.txt`; só entram itens da fonte; traz título e
   data de publicação. Resgata de graça cerca de 23% das fontes sem sitemap gratuito (medição de
   2026-10-15, `scripts/probe_feeds.py`). Limite: o feed só lista os itens mais recentes (mediana
   de 20); entre duas rodadas muito espaçadas (a noite, 16h→06h) pode faltar matéria. Desligue com
   `--no-feed`.
3. **Sitemap pelo Bright Data** (`sitemap_bd`, pago), **só** se o sitemap gratuito foi *bloqueado*
   (401/403/429, ou HTML no lugar de XML). Ausência de sitemap ou erro de rede não paga.

Um host que já precisou do Bright Data fica gravado (`fallback_hosts`): nas rodadas seguintes o
sitemap gratuito nem é tentado, mas o **feed gratuito vem antes de pagar**.

Toda URL inédita é classificada na hora e gravada:

| Tipo | Significado |
|---|---|
| `baseline` | 1ª rodada bem-sucedida do site: histórico, **não** conta como novo |
| `backlog` | inédita, mas mais antiga que `--fresh-days` (padrão 7): arquivo que só agora apareceu |
| `known` | já está no corpus de Mídias IA (hash atual **e** legado); exige `--corpus` |
| `filtered` | descartada pela forma da URL (L0); o motivo fica gravado |
| `new` | o resto: candidata à análise |

### Estratégia por fonte: `probe-sources`

A cascata acima, sozinha, roda do zero a cada rodada e não lembra o que funcionou. O `probe-sources`
descobre, **uma vez por fonte**, qual endereço entrega as matérias novas e grava a escolha (tabela
`midias_crawler_source_strategies`, `sql/postgres/002_...`).

**O `discover` (e, portanto, o `run`) usa principal, reserva e complementos gravados.**
A reserva entra se o principal falhar; os complementos são lidos mesmo quando ele funciona.
A cascata de sempre (sitemap do `robots.txt`, feed, Bright Data) fica como plano B se o conjunto
não render URLs. `--no-strategy` ignora as escolhas e o plano de descoberta. Fonte sem estratégia
segue a cascata. Um host que já precisou do Bright Data não tem a estratégia de sitemap tentada
de graça quando o acesso pago está habilitado. Esta integração não altera o agendamento.
Medição de 2026-10-16 (20 fontes, uma rodada logo após a outra): a estratégia leu 41 sitemaps contra
176 da cascata e achou o que ela deixou passar (g1: +1.258 URLs novas, que a cascata não alcançava
no orçamento de sitemaps; El País: +46, que a cascata via bloqueado); nas outras 17 achou o mesmo.

```bash
scripts/bm.sh probe-sources --only-file data/teste_20_fontes.txt          # grava
scripts/bm.sh probe-sources --only g1.globo.com --dry-run                 # só mostra
# opções: --hints CSV  --max-requests 40 (por fonte)  --workers 8  --interval 1.0
```

Para cada fonte, em etapas (só avança se falta algo), até o orçamento de requisições:

1. **Dicas** (`data/discovery_hints.csv`, do levantamento de 2026-10-07: só um palpite, e só entra
   endereço do próprio domínio e sem credencial), o que o `robots.txt` declara ou, sem declaração,
   `/sitemap.xml` e `/sitemap_index.xml`;
2. se nenhum sitemap de notícias serviu, `/news-sitemap.xml`, `/sitemap-news.xml` e parentes;
3. se nenhum feed serviu, os feeds que a home declara ou os caminhos usuais.

Cada candidato é baixado e **medido** (matérias da fonte, quantas nas últimas 48 h, com data, com
título). Um índice de sitemaps é medido pelos seus melhores filhos (notícias, `lastmod` recente; sem
tags/autores), e é o índice que se grava, porque o endereço dos filhos muda (um por dia). Veredito:
`fresh` (serve), `undated` (sem datas: não prova frescor), `stale` (nada recente), `archive` (lista
enorme quase sem recentes: ler custaria muito e só traria o passado), `empty`, `invalid` (HTML no
lugar de XML), `blocked`, `disallowed`, `unavailable`.

A **nota** é a cobertura recente (teto 1000) × peso do tipo (sitemap de notícias 1,0; feed 0,9;
sitemap comum 0,6) × 0,9 se faltar título; só `fresh` e `undated` pontuam (`undated` vale 0,5).
O de maior nota é o **principal**; a **reserva** é o melhor de *outro tipo* de documento (sobreviver
à falha de um formato). As regras estão em `strategy/scoring.py`, sem aprendizado de máquina. Uma
sondagem só grava a fonte se achou um principal: falha passageira não apaga o que já servia.

#### Cobertura do site e planilha de conferência

Uma fonte só dificilmente lista o site todo (um feed de economia, o sitemap de esporte). Por isso a
sondagem mede, em cada candidato, **quantas seções do site** aparecem entre as matérias recentes
(primeiro segmento do caminho: `sp`, `politica`...) e, entre todos os candidatos frescos, a **união**
das matérias recentes: o "site todo" que dá para ver. Cada candidato recebe a fração da união que
lista (`coverage`). Depois do principal e da reserva, escolhem-se **complementos** (papel
`complement`): a fonte que mais acrescenta matérias ainda não cobertas, até 3, e só se acrescentar
pelo menos 5 matérias e 5% da união. `cover_share` diz quanto da união o conjunto cobre.
A descoberta consome os papéis `primary`, `backup` e `complement` gravados no banco, com
deduplicação das URLs, preservação dos checkpoints dos filhos de todos os sitemaps lidos e
preferência pela data de publicação quando uma mesma URL também aparece apenas com `lastmod`.
Falha em um complemento preserva os resultados dos demais e registra leitura parcial.

O catálogo padrão também usa `data/plano_descoberta.json`, com os mecanismos validados para
Times of India, Respekt, La Stampa, Jeune Afrique e Mediapart. O primeiro mecanismo de cada
fonte é o principal; os demais são complementos. O plano substitui essas escolhas em memória,
preserva reservas já gravadas e não altera a tabela de estratégias. Catálogos personalizados
só usam um plano com `--discovery-plan ARQUIVO`. O formato é uma lista de objetos
`{"chave": "site", "mecanismos": [{"url": "https://site/sitemap.xml", "kind": "sitemap"}]}`;
os tipos aceitos são `sitemap`, `news_sitemap` e `feed`. URLs externas, assinadas ou duplicadas
são recusadas antes do HTTP. Regras manuais de exclusão continuam valendo para mecanismos e
filhos; um principal forçado à mão prevalece sobre o plano padrão. Um plano explícito que
conflite com essa regra é recusado. `--no-feed` também exclui complementos de feed.

Limites de `discover` e `run` para o conjunto gratuito:

- `--max-complements 3`: até três complementos por fonte; `0` desliga a união.
- `--max-source-requests 40`: chamadas à pilha HTTP por fonte, compartilhadas com robots,
  filhos dos sitemaps e fallback gratuito. Redirecionamentos e retentativas internos ficam
  fora dessa contagem.
- `--source-timeout 120`: segundos para iniciar novas chamadas do conjunto; uma chamada em
  andamento termina pelos timeouts existentes. O limite não interrompe a resposta em curso.

Esses dois últimos limites se aplicam às fontes com conjunto de complementos ou exclusões
manuais. Os limites existentes de cada sitemap/feed continuam valendo nas demais fontes.
Esgotar o orçamento ou omitir complementos pelo teto deixa leitura parcial explícita; não
inicia outra exploração de seções. O fallback pago mantém seu teto separado de custo.
Antes de iniciar uma rodada com complementos, o comando exige a migração SQL 007.

```bash
scripts/bm.sh discover --only lastampa.it mediapart.fr --max-complements 2 \
    --max-source-requests 20 --source-timeout 60
```

A validação da integração pelo CLI real usou apenas HTTP gratuito e estado incremental em
um schema descartável do banco de testes: cinco fontes, duas leituras, todas sem falhas ou
leituras parciais. A primeira reuniu 6.053 URLs candidatas, 2.425 adicionais dos complementos;
352 dessas adicionais tinham publicação declarada nas últimas 48 horas. A segunda registrou
zero URLs novas e pulou dois filhos com checkpoint inalterado. Foram 68 chamadas HTTP de
transporte. Isso valida a integração e a deduplicação, sem comprovar cobertura total ou
relevância das notícias. Relatórios: `var/integracao-complementos-piloto.xlsx` e
`var/integracao-complementos-piloto.json`. A migração 007 foi aplicada no Postgres local,
preservando os 180 registros de rodadas existentes; o corpus e o cron não foram alterados.

`export-strategies` gera a planilha para conferir (`var/estrategias.xlsx` por padrão; `.csv` grava
um arquivo por aba):

```bash
scripts/bm.sh export-strategies --only-file data/teste_g1.txt --out var/estrategias-g1.xlsx
scripts/bm.sh export-strategies --out var/estrategias.xlsx       # as 1.294 fontes do catálogo
```

Abas: **Estratégia** (uma linha por fonte, inclusive as não sondadas e as sem estratégia, com o
motivo e, para as lentas, a data da última matéria), **Candidatos** (tudo o que foi baixado e medido,
com veredito, seções e cobertura), **Seções** (as seções mais frequentes de cada candidato escolhido)
e **Mapa de seções** (uma linha por seção vista em qualquer candidata da fonte, com a maior contagem,
se o conjunto escolhido a lista e em qual papel; em site de URL plana, em que o primeiro trecho do
caminho muda a cada matéria, só o aviso, porque ali não há seção para mapear). As colunas "Ref."
trazem o que o levantamento de 07/10 estimou (categoria, itens/dia no feed, artigos/dia no GDELT) só
para você comparar com a "Estimativa por dia" (união de 48 h ÷ 2). Precisa do extra `xlsx`
(`pip install -e '.[xlsx]'`).

**Dicas manuais** (`data/hints_manuais.csv`, além do levantamento): feeds que a home não declara e só
quem conhece o site sabe, como os do g1 (`/rss/g1/`, por editoria e por estado, `/rss/g1/ac/acre/`).
A sondagem os mede como qualquer candidato. Uma dica de fonte de seção pode ficar fora do prefixo
dela (o feed do g1 Acre não está sob `/ac/acre`): vale o domínio, e as matérias são conferidas uma
a uma contra o prefixo. Os CSVs de dicas toleram colunas alinhadas por espaços (editores fazem isso).

**Correções à mão** (`data/estrategias_manuais.csv`: `dominio,endereco,acao,motivo`): onde a sondagem
erra e você sabe, `excluir` tira um endereço da sondagem (com `*` no fim vale o prefixo:
`https://www.nature.com/subjects/*`) e `forçar` manda usá-lo como principal (se servir). Vale a cada
rodada. Uma ação desconhecida aborta o `probe-sources` com a linha do erro, para uma correção com
erro de digitação não ficar sem efeito em silêncio. A planilha `var/revisar-nao-noticia.xlsx` lista
as fontes cujo principal parece listagem (tags, categorias, produtos), com 5 URLs de amostra.

**Varredura de listagens (`listing`), o último recurso:** para a fonte sem sitemap nem feed que sirva
(nem uma lenta), a sondagem varre a home e as páginas de seção. Não há padrão único de URL entre os
sites, mas há padrões reconhecíveis (`strategy/listing.py`): matéria é um *slug* longo, ou o caminho
traz data, ou um número de identificação; seção é um caminho curto de palavras (`/politica`,
`/ac/acre`). A varredura parte da home, entra nas páginas de seção e, de cada uma, colhe matérias e
**novas seções** (a URL de uma seção ajuda a achar as outras), até 10 páginas por fonte, respeitando o
`robots.txt`. Menos de 8 links de matéria não é estratégia. Não há data nem título: o veredito é
`undated` e a nota é a mais baixa de todas, então qualquer sitemap, feed ou fonte lenta vence.
O que se grava é a home; as seções vistas aparecem no mapa de seções.

**Zip como categoria de leitura:** um sitemap pode vir em ``.xml``, ``.xml.gz`` ou ``.zip`` (de um único
XML); o zip é lido com o mesmo limite de tamanho do gzip (a bomba de descompressão vale para o que
se lê, não para o tamanho declarado) e um zip com vários arquivos é recusado, em vez de ler só um.
A exclusão manual vale também para os filhos de um índice (`https://www.seudinheiro.com/cotacoes-sitemap.xml`).

**Filtro de forma (L0):** um termo de listagem (`category`, `page`, `tag`, `product`...) só descarta a URL
se quase nada vem depois dele (`/tag/cars`, `/politica/page/2`). Com mais caminho
(`/category/world/2026/10/17/titulo`, `/page/202610/123.shtml`) é matéria: antes, sites como
`freemalaysiatoday.com` e `globaltimes.cn` tinham todas as matérias descartadas.

**Regras da sondagem que vieram de sites reais:** um sitemap de tags, categorias, autores ou produtos
no topo nunca é baixado (só um endereço forçado); um sitemap declarado no `robots.txt` que falha ou
dá 404 ganha uma segunda tentativa (os adivinhados não); uma lista grande só é "arquivo" se tiver
poucos recentes **em número** (menos de 100) além de em proporção (menos de 5%); um índice de feeds
vale como feed; um índice com várias famílias de arquivos (regiões, temas, categoria por ano) lê o
primeiro filho de cada, até 16, e um índice de uma família só (arquivos do dia ou do mês) continua
lendo os 3 mais recentes.

**Fontes do mesmo host** (as dez do g1) baixam cada arquivo uma só vez por rodada de `probe-sources`
(`net/memoizing.py`, só nesses hosts, para não guardar em memória o resto do catálogo).

#### Relatório de quebras (`strategy-health`)

Só leitura: mostra o que o humano precisa ver, sem esperar a vez da reavaliação.

```bash
scripts/bm.sh strategy-health                       # no terminal
scripts/bm.sh strategy-health --out var/saude.csv   # e em CSV
```

- **QUEBROU:** a leitura falhou nas últimas rodadas desde a sondagem, ou a sondagem mais recente não
  achou padrão e a estratégia antiga segue gravada (ela ainda é usada, mas ninguém sabe se serve).
- **ATENÇÃO:** o padrão funciona, mas mudou: sem URL nova por várias rodadas numa fonte que rendia,
  ou a lista encolheu muito.
- Uma fonte que nunca teve padrão não "quebrou": aparece só em "sem estratégia" no resumo.

A reavaliação automática (abaixo) tenta consertar sozinha; o relatório é o que sobra depois dela.

#### Reavaliação automática (`probe-sources --due`, o passo 0 do `run`)

Antes de cada descoberta o `run` reavalia **só as fontes na vez** (poucas por rodada). A regra está
em `strategy/health.py`, com parâmetros em `HealthPolicy`, e olha só as rodadas **desde a última
sondagem**:

| Motivo | Quando |
|---|---|
| `never_probed` | a fonte nunca foi sondada (estratégias gravadas antes do controle são adotadas com a idade da sondagem) |
| `refresh` | a estratégia tem mais de 7 dias (endereços mudam) |
| `retry` | a fonte não tinha estratégia e a espera acabou |
| `failing` | as 2 últimas rodadas falharam (bloqueio, erro, sem sitemap) |
| `silent` | 4 rodadas `ok` sem nenhuma URL nova, numa fonte que antes rendeu 10 ou mais |
| `shrunk` | a rodada listou menos de 25% da mediana das anteriores (mediana ≥ 20; rodadas com sitemaps pulados não entram: o volume não é comparável) |

**Espera crescente:** sondagem sem achar nada, ou por saúde sem mudar a escolha, dobra a espera até
a próxima reavaliação por saúde (6 h, 12 h, 24 h... até 7 dias), então uma fonte quieta por natureza
não é sondada a cada rodada. Reavaliação por idade ou manual zera a espera; escolha nova também. A
sondagem só troca a estratégia gravada se achou um principal. O estado fica em
`midias_crawler_source_probe_state` (`sql/postgres/003_...`).

**Endereços com data embutida** (um sitemap por dia ou mês, como `.../sitemap/2026-10-15/` ou
`sitemap-2026-10.xml`) viram modelo (`{YYYY}-{MM}-{DD}`) quando a data é recente (até 45 dias; mês
corrente e dois anteriores), e a descoberta lê o de hoje e o de ontem (o dia vira em horas
diferentes por fuso). Arquivos de ano antigo não viram modelo. Em um índice sem `lastmod`, os filhos
se ordenam pela data da URL (e `latest`/`recent` vêm antes): era o que fazia o índice por dia do
Estadão (9.951 filhos, do mais novo ao mais antigo) ser lido do fim, nos anos 1999.

### Filtro pela forma da URL (L0)

Descarta só o que certamente não é matéria: `home` (caminho vazio), `listing` (segmento `tag`,
`author`, `category`, `keyword`, `topic`, `page`, `search`...), `section` (um único segmento só com
letras, como `/entertainment`) e `asset` (imagem, vídeo, XML). Na dúvida a URL passa. Roda apenas
sobre URLs inéditas e frescas (linha de base e backlog não são filtrados).

`--skip-off-topic` também descarta seções de temas sem interesse (esporte, culinária, horóscopo).
Fica **desligado por padrão**: uma seção de esporte também traz matérias de gestão e denúncia
(ex.: dirigentes, loterias, entidades esportivas). Audite antes de ligar: `report` mostra o total
descartado por motivo.

### Como os sitemaps são escolhidos

- Os sitemaps declarados no `robots.txt` (ex.: o de notícias) são lidos **antes** dos filhos de um
  índice; os filhos usam só o orçamento que sobra.
- Filhos que são listagens (`post_tag`, `category`, `author`, `keyword`, `users`, páginas estáticas,
  imagens) não são lidos enquanto houver sitemap de matérias.
- Se algum sitemap falha mas o site ainda rende URLs, o site fica `ok` com **leitura parcial**:
  `report` mostra quantos sitemaps falharam e o motivo fica em `detail`.

- Sitemaps filhos com `lastmod` inalterado desde a última leitura são pulados (sem requisição).
- Hosts que já bloquearam ficam gravados e vão direto ao Bright Data nas rodadas seguintes.
- `--corpus` usa `DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD` e abre a sessão em modo
  somente leitura. A linha de base não consulta o corpus (é só histórico).
- `report` mostra, por rodada, novas/linha de base/backlog/no corpus, a latência entre publicação
  (news sitemap) e descoberta, e os sites que nunca trouxeram nada inédito (sitemap parado).

## Enriquecimento: texto, contexto, critério e evidência

Mesma ordem do KyX (primeiro o contexto, e a captura só depois de haver entidade): para cada URL
`new` ainda sem análise,

1. **Texto por HTTP direto** (educado: `robots.txt`, intervalo por host, sem Bright Data). Site que
   bloqueia, matéria que sumiu ou página inutilizável não impedem a análise: ela segue só pela
   manchete (`basis=headline`, confiança limitada a 0,6). Erro de rede é retentado (até 3 vezes).
2. **Análise de contexto** pelo OpenRouter (`google/gemini-2.5-flash-lite`): entidades, temas e
   fato adverso. As **entidades** (pessoas e organizações nomeadas, nunca país ou cidade) são
   listadas em toda matéria e só valem se **constarem no texto fornecido**; nomes inventados ou
   parafraseados são descartados. O **achado adverso** (`finding=true`) é uma marca à parte e
   continua exigindo uma entidade nomeada com papel adverso (acusado, investigado, condenado, réu,
   responsável); autoridade, vítima e "citado" não bastam.
3. **Critério** (`src/buscador_midias/criteria.py`, a única regra de "entra ou não"): a análise
   **leu o texto** (`basis=text`) **e** achou ao menos uma entidade. Existe em Python e em SQL, com
   um teste que garante que concordam. Análise só pela manchete não atende (evita pagar evidência
   por matéria que não vai ao corpus); em 2026-10-16 eram 19 de 904 (2%).
4. **Evidência** só das que atendem ao critério (`midias-ia/evidencias/<url_hash>.html`, como no
   KyX). O HTML do download direto é reaproveitado se for confiável. O **Bright Data (pago) só
   entra para o achado adverso**; a matéria neutra cujo HTML direto não serve fica sem evidência
   (o artigo é gravado mesmo assim, com a evidência como `failed`).

A evidência é capturada bloco a bloco, logo depois da análise do bloco, e o HTML é solto em seguida
(com milhares de matérias por dia, acumular as páginas estouraria a memória).

```bash
.venv/bin/python -m buscador_midias enrich                       # reaproveita o HTML se for confiável
.venv/bin/python -m buscador_midias enrich --evidence-mode brightdata   # só Bright Data
.venv/bin/python -m buscador_midias enrich --limit 40 --bright-data-max-requests 10
```

### Quando o HTML do download direto serve de evidência

Em `--evidence-mode auto` o HTML é reaproveitado **só se passar em todos os critérios**
(`extraction/reliability.py`); qualquer reprovação grava o motivo e cai para o Bright Data, cuja
cópia **passa pelos mesmos critérios** (um provedor também pode devolver uma página de erro):

| Código | O que reprova |
|---|---|
| `http_status` / `not_html` / `too_small` | status ≠ 200, tipo não HTML, página minúscula |
| `error_page` / `challenge_page` | "não encontrado" com status 200 (regra do KyX), desafio de bot/CAPTCHA |
| `redirected_away` | o site redirecionou para outro domínio ou para a home |
| `title_mismatch` | o título da página não cobre 60% da manchete esperada |
| `not_article` | sem JSON-LD de artigo, `og:type=article` nem data de publicação em `meta` (seção/listagem) |
| `thin_content` / `paywall` | menos de 500 caracteres de texto, ou aviso de assinatura com texto curto |
| `entity_missing` | nenhuma entidade adversa da análise aparece na página |

O conteúdo gravado leva `<base href>` (como no KyX), `sha256` e tamanho; o estado guarda a
origem (`direct`/`brightdata`), o status HTTP e os motivos de rejeição. Se nada passar, a evidência
fica `failed` (retentada até 3 vezes).

`scripts/eval_evidence_reuse.py` mede isso na prática (baixa direto e pelo Bright Data e compara as
duas cópias; faz até 1 chamada paga por URL; `--source recent` amostra URLs recentes de fontes
diferentes, já sem o que o L0 descarta; `--source new` amostra as URLs novas). Resultados (jornais de 60 fontes de ~40 idiomas):

- **Nenhum falso aceite:** todas as páginas aceitas no direto e confiáveis no Bright Data eram
  idênticas (similaridade 1,00, mesmo título, data e tamanho), em mais de 60 comparações.
- Em 2 casos o Bright Data é que falhou (cookie wall, `not_html`) e o HTML direto era a matéria.
- Só as fontes que bloqueiam (maisgoias, 38north) precisaram do Bright Data.
- O teste em escala achou três defeitos nos critérios, já corrigidos: `title_mismatch` em páginas
  de outro alfabeto (slug transliterado; hoje compara por letras Unicode, aceita a URL canônica
  igual à pedida e não compara quando os alfabetos diferem), `thin_content` por texto fora de
  `<p>` (hoje cai para o texto do `<article>`/`<main>`/corpo) e o limite de 500 caracteres, que
  punia chinês, japonês e coreano (150 quando mais de 30% do texto é CJK). O aceite no direto
  subiu de 33 para 46 de 60, sem regressões, e as 13 páginas recém-aceitas foram confirmadas
  idênticas pelo Bright Data.
- Rejeições que ficam, de propósito: seções, listagens, páginas de termos, home, paywalls e
  matérias vazias por JavaScript (nos dois lados).

## Gravação no corpus de Mídias IA

`corpus-sync` grava direto no Postgres do corpus (`pgsql_midias`) toda matéria que atende ao
critério (lida e com ao menos uma entidade identificável, adversa ou não). Não usa a fila do KyX:
o consumer de persistência de lá exige um `RunContext` (entidade e tema pesquisados), que a varredura de sitemaps não tem.

```bash
# DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD vêm do ambiente, como no KyX
.venv/bin/python -m buscador_midias corpus-sync --dry-run   # só conta; não grava nada
.venv/bin/python -m buscador_midias corpus-sync             # grava
.venv/bin/python -m buscador_midias corpus-sync --limit 20
```

- **Linhas gravadas:** `midias_ia_articles` (`origin='crawler'`, `body_source='crawler_html'`,
  `ai_classified=true`), `midias_ia_article_entities` (PER/ORG, papel mapeado para o vocabulário do
  KyX, `extractor='buscador-midias'`) e `midias_ia_article_keywords` (nome do tema do KyX em
  minúsculas, por exemplo `pld/ft`). Cada artigo é uma transação própria.
- **Categoria "Diversos":** matéria sem tema adverso (e a adversa só com tema desconhecido) recebe
  a palavra-chave `diversos`. A busca do KyX casa entidade **e** o nome do tema em minúsculas com as
  palavras-chave do artigo, então basta existir no KyX um tema chamado **Diversos** para essas
  matérias aparecerem. Sem esse tema, elas ficam no corpus mas nenhuma busca atual as encontra.
- **`ai_verdict`:** só o fato adverso vira `relevante`. Matéria neutra vai como `incerto`: o KyX
  trata `ruido` como "não relevante" no veredito final, e quem decide por entidade e tema é o
  Treatment dele.
- **Idempotente:** a chave é `url_hash` (`ON CONFLICT DO NOTHING`). Rodar de novo não duplica. Artigo
  de origem própria tem evidência, entidades e temas atualizados; artigo que veio de outra origem só
  ganha a evidência se ainda não tinha.
- **Linha que sumiu:** antes de pular um achado já gravado, o comando confere se a linha ainda
  existe no corpus e a refaz se não existir.
- **Falha de esquema ou conexão** aborta antes de gravar (código 2) e nunca imprime a senha.
- **Evidência:** no ambiente local o arquivo fica em `var/evidence/` (`MIDIAS_IA_STORAGE_DISK=local`);
  nos demais vai para o S3 pelas mesmas variáveis `AWS_*` do KyX. O adaptador S3 só foi testado com
  cliente falso, não contra um bucket real.
- Cuidado: testes do KyX que chamam `truncate` no corpus local já apagaram esses dados antes; o
  estado desta POC (`corpus_writes`) sabe disso e refaz as linhas que sumirem.

## Rotulagem às cegas (precisão e recall)

Para saber se a análise acerta, uma pessoa julga uma amostra sem ver o veredito do
modelo. **A pergunta é "esta notícia entra no corpus?"**, isto é, é uma matéria que nomeia ao menos
uma pessoa ou organização identificável (boa, ruim ou neutra). Não é "é adversa?". Três comandos,
nesta ordem:

```bash
.venv/bin/python -m buscador_midias labels export --per-group 70   # sorteia o lote (var/labels/)
.venv/bin/python -m buscador_midias labels serve                   # tela em http://127.0.0.1:8766
.venv/bin/python -m buscador_midias labels score                   # precisão e recall estimados
```

- **`export`** sorteia URLs de dois grupos (as que o modelo mandou ao corpus e as lidas sem
  entidade), embaralha tudo e grava `rotulagem-NN.csv` (o que a pessoa
  vê) e `chave.csv` (grupo, temas e entidades do modelo, usado só na pontuação). Não repete URLs já
  exportadas. Lotes de antes da mudança de critério (grupos `finding`/`kept_no_finding`, ou
  `kept_no_entity`/`triage_discarded`) não batem com os grupos de hoje: guarde-os numa pasta à
  parte e exporte de novo.
- **`serve`** abre a tela: uma notícia por vez (fonte, link da matéria, manchete e trecho) e três
  botões, **Entra no corpus**, **Não entra** e **Incerto**, também pelas teclas `S`, `N`, `I` (ou
  `1`, `2`, `3`). O CSV recebe `sim`, `nao` ou `incerto` (as palavras antigas `adverso` e
  `nao_adverso` não valem mais: respondiam a outra pergunta). Cada clique grava na hora no CSV e vai para a próxima sem rótulo. Dá para voltar
  (`←`), pular (`→`) e limpar um rótulo. Pode fechar e voltar depois: ela continua de onde parou.
  A tela nunca abre `chave.csv`, então o veredito do modelo não aparece em lugar nenhum dela.
- **`score`** lê os mesmos CSVs, então dá para misturar a tela com o preenchimento no Excel.
  "Incerto" fica fora das taxas. Sai a precisão do corpus e o recall estimado da análise (taxa
  de "entra" de cada grupo × tamanho do grupo no estado);
  vale tanto quanto o tamanho da amostra: veja o intervalo de 95% no relatório.
- A tela **grava**, ao contrário do painel (só leitura). Por isso escuta só em `127.0.0.1`, recusa
  `Host` que não seja local, `Origin` de outro site e qualquer voto que não seja JSON, e o veredito
  é uma lista fechada. Não exponha a porta para a rede.
- Cada gravação troca o arquivo inteiro (temporário + renomear): uma queda no meio não deixa CSV
  pela metade. Evite editar o mesmo lote no Excel enquanto a tela está aberta (quem salvar por
  último vence).

## Execução agendada

`run` encadeia descoberta, análise (com o critério e a evidência) e gravação no corpus. Cada etapa
é o subcomando de mesmo nome, então as opções valem aqui; uma etapa que falha não impede as
seguintes (código de saída 1 se alguma falhou). `var/run.lock` impede duas execuções ao mesmo
tempo (a segunda sai com código 3).

```bash
scripts/run.sh --only-file data/poc_sample.txt           # uma execução agora
scripts/run.sh --no-bright-data --limit 10               # teste barato, sem gasto pago
scripts/install_cron.sh                                  # 06:00, 12:00 e 16:00 (fuso da máquina)
scripts/install_cron.sh --times 07:00,13:00 --dry-run    # só mostra o crontab resultante
scripts/install_cron.sh --remove                         # tira o bloco do buscador do crontab
tail -f var/run.log                                      # saída das execuções do cron
```

- `scripts/run.sh` (e `scripts/bm.sh <subcomando>` para qualquer outro) lê as credenciais do `.env`
  do KyX (`KYX_ENV` muda o caminho), só as variáveis que o buscador usa e sem imprimir nada (nunca
  com `eval`); o que já está no ambiente vence o arquivo. O estado e o corpus são o mesmo Postgres
  (`DB_MIDIAS_IA_*`, host `127.0.0.1`; `CORPUS_DB_HOST` troca).
- Tetos de custo por execução: `--discovery-bright-data-max` (60 chamadas) e
  `--enrich-bright-data-max` (100). `--no-bright-data` desliga o pago em tudo.
- `--enrich-workers` (padrão 8) paraleliza downloads e chamadas ao LLM; a fila de análise intercala as
  fontes para o intervalo de 1 s por host não travar um bloco (cerca de 90 URLs por minuto).
- `install_cron.sh` reescreve só o bloco `# >>> buscador-midias >>>` do crontab e grava
  `var/schedule.json` (`{"daily": [...]}`) para a agenda do painel.
- **Agendamento desativado em 2026-10-18:** execuções apenas manuais. `install_cron.sh --remove`
  também desativa a agenda do painel, sem estimar próximas rodadas pelo histórico.
- **Teste de 20 fontes (2026-10-16):** o cron estava apontado para `data/teste_20_fontes.txt` (10
  brasileiras e 10 estrangeiras conhecidas) com `--max-sitemap-fetches 12` (BBC e R7 declaram vários
  índices no `robots.txt` e o orçamento padrão de 6 acabava neles). Painel: `scripts/bm.sh dashboard`.
- **Escopo antigo:** o cron usava a amostra da POC (`data/poc_sample.txt`, 125 fontes). Ampliar para as
  1294 fontes exige antes decidir a linha de base (cada fonte nova grava milhares de URLs de
  histórico).
- **Backup:** o estado é do Postgres; o backup é o do banco (`pg_dump`), não mais um comando do
  buscador.

## Painel em tempo real

```bash
.venv/bin/python -m buscador_midias dashboard            # http://127.0.0.1:8765
.venv/bin/python -m buscador_midias dashboard --port 9000
```

Servidor local, **somente leitura** (sessão do Postgres com `default_transaction_read_only=on`), sem
dependências novas e sem recursos externos. A página consulta `/api/snapshot` a cada 5 s e mostra:
estado da descoberta (com progresso da rodada em curso) e do enriquecimento, novas nas últimas 24 h,
custo da análise, latência de publicação, funil acumulado, atividade por hora, rodadas (com a fonte
de cada site: sitemap, feed ou Bright Data), execuções do enriquecimento e do corpus, descartes por
motivo, fontes (problemas primeiro, com filtro, e a via de cada uma) e o feed das últimas URLs
novas com a situação da análise.

- **Agenda e linha do tempo:** a agenda vem de um JSON opcional (`--schedule`, padrão
  `var/schedule.json`, relido a cada atualização), que o `install_cron.sh` escreve. Sem o arquivo, o painel estima a próxima rodada pela cadência observada
  (precisa de 3+ rodadas com intervalo mediano ≥ 30 min) e avisa que é estimativa. Estados:
  `concluída` (houve execução do tipo na janela do horário), `agendada`, `no horário` (até 10 min)
  e `atrasada`.
  ```json
  {"daily": ["06:00", "12:00", "16:00"],
   "jobs": [{"name": "Rodada extra", "kind": "discovery",
             "at": "2026-10-15T17:22:00-03:00", "note": "aprox."}]}
  ```
  `daily` são horários que se repetem todo dia no fuso da máquina (como o cron); `jobs` são
  tarefas avulsas (`kind` é `discovery`, `enrich` ou `corpus`; `at` precisa de fuso). Entradas
  inválidas são ignoradas.
- **Bright Data:** cada rodada grava quantas chamadas pagas fez e quantos hosts dependeram dele;
  o painel mostra por rodada e o total acumulado (o `report` também). É contagem de chamadas, não
  dinheiro: o preço por chamada não é oficial.
- **Desempenho:** as contagens por fonte e tipo vêm de contadores mantidos a cada gravação
  (`site_kind_counts`) e as consultas de novas usam o índice `(kind, first_seen_at)`; o retrato
  leva ~25 ms com 1,4 milhão de URLs e é reaproveitado por 4 s entre as requisições (várias abas
  não recalculam). Bancos antigos ganham contadores e índice ao abrir em modo de escrita.
- Escuta só em `127.0.0.1` por padrão: **não tem autenticação**; não exponha na rede.
- A página HTML é lida na subida do servidor: reinicie o painel depois de editá-la.
- Uma rodada sem `finished_at` há mais de 3 h aparece como "interrompida?".

## Estado no Postgres

Todo o estado (URLs vistas, rodadas, análises, evidências, gravações) vive nas tabelas
`midias_crawler_*` do banco `pgsql_midias`, o mesmo do corpus. A conexão é a de sempre
(`DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD`); não há mais `--db` nem arquivo SQLite.

- **Esquema:** `sql/postgres/001_create_midias_crawler_tables.sql` (13 tabelas) e
  `002_create_midias_crawler_source_strategies.sql` (a estratégia por fonte) e
  `003_create_midias_crawler_source_probe_state.sql` (quando reavaliar e por que falhou); `999_...`
  desfaz tudo. O inventário de seções usa `004_create_midias_crawler_section_pages.sql` e a
  conferência de mecanismos usa `005_create_midias_crawler_section_sources.sql`.
  Os campos de evidência do tipo e origem da candidata estão em
  `006_add_midias_crawler_page_classification.sql`. A origem combinada da descoberta exige
  `007_allow_combined_discovery.sql`, que amplia somente o CHECK de `site_runs.source`,
  sem alterar registros ou tabelas do corpus.
  O buscador nunca cria nem altera tabelas: se faltarem, ele diz qual arquivo rodar. O mesmo DDL deve
  virar a migration do powerdd-backend. Tipos: datas em `timestamptz`, `themes` e `rejected_reasons`
  em `text[]`, `entities` em `jsonb`, `finding` e `created` em `boolean`.
- **Estado antigo em SQLite:** `scripts/bm.sh migrate-sqlite var/state-teste.db` copia o arquivo para
  as tabelas (só para um destino vazio; mantém os ids das rodadas e a ordem de chegada das URLs;
  as tabelas da triagem e `corpus_runs.withheld` não migram). Em 2026-10-16 foram migradas as
  251.579 URLs do teste de 20 fontes; o `var/state.db` das 125 fontes **não** foi migrado (fica como
  arquivo morto).
- **Testes:** usam o banco `buscador_midias_test` (nunca o do corpus) no mesmo servidor, com um
  schema novo por teste criado pelo mesmo DDL (`tests/conftest.py`). Sem o Postgres no ar, os testes
  que precisam do estado falham com a mensagem de conexão.

## Catálogo de fontes

`data/sites.csv` é exportado do seed do `powerdd-backend` (tabela `midias_ia_trusted_sources`):

```bash
.venv/bin/python scripts/export_sites.py   # imprime linhas, únicas, duplicatas e seções
```

- O CSV é uma exportação fiel (1299 linhas); `load_sites` deduplica por `host + prefixo de seção`
  (1294 únicas, 5 duplicatas).
- 63 fontes são **seções** de um domínio (ex.: `g1.globo.com/globonews`): só URLs sob o prefixo
  pertencem a elas (`Site.owns`).
