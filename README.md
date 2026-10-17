# buscador-midias

POC de um sistema separado que varre as 1294 fontes de notícias confiáveis da Mídias IA e alimenta
o corpus (Postgres) e o armazenamento de evidências. A meta é aposentar o ramo de busca na web do
pipeline de Mídias IA. Corpus **por entidade, não por adversidade**: entra toda matéria que nomeia
uma pessoa ou organização identificável (a categoria "Diversos" do KyX recebe a neutra).

## O fluxo (3 vezes ao dia: 06h, 12h e 16h)

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
scripts/bm.sh map-sites --retry-failed --only folha.uol.com.br  # nova tentativa das falhas
# Atualiza a amostra da página e confere mecanismos (DDL 005 aplicado):
scripts/bm.sh map-sites --only g1.globo.com --refresh-pages --pages-per-site 12 \
    --validate-sources --source-requests 20 --out var/mapa-sites-piloto.xlsx
scripts/bm.sh map-sites --only g1.globo.com --sources-only --source-requests 20
# Consome as filas sem exigir uma confirmação para cada rodada:
scripts/bm.sh map-sites --drain --retry-failed --validate-sources \
    --pages-per-site 100 --source-requests 20 --workers 24 --out var/mapa-sites-completo.xlsx
```

Cada página processada grava um checkpoint atômico com as novas pendências no Postgres. Home e
candidatas a seção são exploradas seguindo novas seções. A entrada vem primeiro, depois as
candidatas apontadas explicitamente na navegação e, por último, os palpites pelo formato da URL;
dentro de cada grupo, páginas mais rasas primeiro. Guarda endereço, primeira
página ou mecanismo que o apontou, título, links com aparência de matéria, feeds/sitemaps apontados, índices
de feeds, tentativas e falhas. Tags, autores e diretórios de tópicos (`topic`, `topics`,
`topics-list`, `themen`, `tagg`, `amnen`, `persons`) ficam como taxonomias excluídas para revisão.
Essa classificação também é
aplicada às filas antigas ao retomá-las, preservando os registros e as amostras já salvas.
Uma URL de entrada cadastrada nesses caminhos continua sendo lida.
Rotas com três segmentos iguais consecutivos (`category/category/category`, `index/index/index`)
são preservadas como possível ciclo de navegação,
sem continuar expandindo os links relativos. Seções profundas sem esse padrão continuam elegíveis.
O limite de páginas vale **por lote** (inclui páginas proibidas; robots é uma requisição extra).
A execução usa somente HTTP gratuito, respeita robots e intervalo por host; não chama LLM,
Bright Data, enriquecimento ou escrita no corpus.

Com **`--drain`**, `--pages-per-site` é o tamanho de cada lote interno: a fonte continua até
esgotar as pendências reconhecidas ou não conseguir avançar, mantendo as pendências bloqueadas
no inventário. Falhas e releituras só entram no primeiro lote interno. As fontes são independentes:
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
subseção explicitamente permitida. Falha ao baixar a política mantém a fila suspensa.

Três falhas consecutivas de conexão na mesma origem (DNS, TLS, timeout ou transporte),
dentro do mesmo lote de páginas ou passagem de auditoria,
suspendem a passagem e preservam as páginas ainda não tentadas. O motivo fica na entrada;
uma nova execução pode retomar a fila. A auditoria aplica a mesma regra aos mecanismos e
não reinicia essa passagem automaticamente. Respostas 404 e proibições de caminho não
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
Paginação HTML em `/index/feed/pagina-N` recebe `html_pagination`: o nome do caminho não
a transforma em feed, nem dispara uma varredura do arquivo histórico. Índices desse formato
registrados antes da correção deixam de expandir a fila. XML válido continua sendo medido.
Feeds cujos links apontam exclusivamente a comentários (`#comment-N` ou `replytocom`) recebem
`comments_feed`, preservando as observações e sem comprovar cobertura de notícias. Em feeds
mistos esses links também não entram na comparação canônica. A palavra `feed` no meio de um
título de matéria não basta para descobrir um mecanismo.

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
`var/mapa-sites.xlsx` e `var/mapa-resumo.json` guardam essa fotografia; a fila de continuidade
em `var/mapa-secoes-proximo.txt` contém 791 fontes com leitura válida e pendências.
`var/mapa-revisao-entradas.csv` mantém 299 entradas para revisão e
`var/mapa-retentativas-proximo.txt` separa as falhas transitórias fora da fila principal.

Cada rodada devolve só o que ainda não vimos. O estado vive no **Postgres** (tabelas `midias_crawler_*`, veja "Estado" abaixo).

```bash
.venv/bin/python -m buscador_midias discover --only-file data/poc_sample.txt \
    --bright-data --bright-data-max-requests 60 --corpus
.venv/bin/python -m buscador_midias report
```

### Cascata de fontes: sitemap, feed e Bright Data

Para cada site, a primeira fonte que traz URLs encerra a tentativa (`site_runs.source` guarda qual):

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

**O `discover` (e, portanto, o `run` e o cron) já usa a escolha:** para cada fonte tenta primeiro o
principal, depois a reserva, e só então a cascata de sempre (sitemap do `robots.txt`, feed, Bright
Data) como plano B. Se o endereço escolhido mudou ou quebrou, a fonte não fica sem descoberta.
`--no-strategy` (no `discover` e no `run`) ignora a escolha. Fonte sem estratégia gravada segue só a
cascata. Um host que já precisou do Bright Data não tem a estratégia de sitemap tentada de graça.
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
pelo menos 5 matérias e 5% da união. `cover_share` diz quanto da união o conjunto cobre. **A
descoberta ainda lê só o principal e a reserva**; ler o conjunto (principal + complementos) é o
próximo passo.

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
- **Teste de 20 fontes (2026-10-16):** o cron está apontado para `data/teste_20_fontes.txt` (10
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
  `006_add_midias_crawler_page_classification.sql`.
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
