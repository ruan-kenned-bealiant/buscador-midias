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
| 1. Buscar notícias novas | sitemap → feed → sitemap pelo Bright Data (cascata) | `discover` |
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

Cada rodada devolve só o que ainda não vimos. Estado em SQLite (`var/state.db`).

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

## Execução agendada e backup

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

- `scripts/run.sh` lê as credenciais do `.env` do KyX (`KYX_ENV` muda o caminho), só as variáveis
  que o buscador usa e sem imprimir nada (nunca com `eval`); o que já está no ambiente vence o
  arquivo. O corpus é o Postgres local (`CORPUS_DB_HOST` troca o `127.0.0.1`).
- Tetos de custo por execução: `--discovery-bright-data-max` (60 chamadas) e
  `--enrich-bright-data-max` (100). `--no-bright-data` desliga o pago em tudo.
- `install_cron.sh` reescreve só o bloco `# >>> buscador-midias >>>` do crontab e grava
  `var/schedule.json` (`{"daily": [...]}`) para a agenda do painel.
- **Escopo:** o cron usa a amostra da POC (`data/poc_sample.txt`, 125 fontes). Ampliar para as 1294
  fontes exige antes decidir a linha de base (cada fonte nova grava milhares de URLs de histórico).

```bash
.venv/bin/python -m buscador_midias backup        # var/backups/state-AAAAMMDD-HHMMSS.db (UTC)
```

`backup` usa a API de backup do SQLite (cópia consistente com o banco em uso), grava um arquivo
único, confere a integridade e nunca sobrescreve uma cópia existente. `var/` não vai para o git.

## Painel em tempo real

```bash
.venv/bin/python -m buscador_midias dashboard            # http://127.0.0.1:8765
.venv/bin/python -m buscador_midias dashboard --port 9000
```

Servidor local, **somente leitura** (abre o SQLite com `mode=ro` a cada requisição), sem
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
- O comando `dashboard` atualiza uma vez o esquema de um estado de versão anterior antes de abri-lo
  somente leitura.

## Catálogo de fontes

`data/sites.csv` é exportado do seed do `powerdd-backend` (tabela `midias_ia_trusted_sources`):

```bash
.venv/bin/python scripts/export_sites.py   # imprime linhas, únicas, duplicatas e seções
```

- O CSV é uma exportação fiel (1299 linhas); `load_sites` deduplica por `host + prefixo de seção`
  (1294 únicas, 5 duplicatas).
- 63 fontes são **seções** de um domínio (ex.: `g1.globo.com/globonews`): só URLs sob o prefixo
  pertencem a elas (`Site.owns`).
