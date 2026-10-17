# Design

## Objetivo

Substituir a busca ao vivo na web por uma coleta proativa, três vezes ao dia (06h, 12h e 16h):
buscar notícias novas nos sites, analisar o contexto, conferir o critério, capturar a evidência e
gravar no corpus (`midias_ia_*`, Postgres `pgsql_midias`) e no bucket.

## Decisões

- **Descoberta em cascata, da mais barata à mais cara:** sitemap gratuito, feed RSS/Atom gratuito e,
  só para quem bloqueia, sitemap pelo Bright Data. (A escolha inicial era "só sitemap"; a medição
  de 2026-10-15 mostrou que o feed resgata de graça ~23% das fontes sem sitemap gratuito.)
- **O corpus é por entidade, não por adversidade:** entra toda matéria com ao menos uma pessoa ou
  organização identificável (adversa ou não); só se descarta o que não tem quem identificar. O fato
  adverso é uma marca (`finding`, temas), nunca um filtro. O veredito por entidade e tema
  (`match_nome`, `alerta`) continua no Treatment, na hora da DD, e a matéria sem tema adverso é
  achada no KyX pela categoria "Diversos" (palavra-chave `diversos`).
- **Sem triagem por manchete.** Havia L0 (forma da URL), L1 (Naive Bayes) e uma triagem por LLM;
  com o critério por entidade a triagem descartava ~2% das URLs e custava mais do que poupava, e o
  L1 nunca entrou no fluxo. Sobrou só o L0, que é barato e certeiro. A decisão de verdade é a
  análise do texto.
- **Estratégia por fonte, descoberta e lembrada (`strategy/`):** a cascata estática (sitemap, feed,
  pago) não sabe qual endereço serve cada fonte e relê arquivos inúteis (Le Monde: 105 mil URLs de
  arquivo no lugar do sitemap de notícias de ~250 itens). `probe-sources` baixa os candidatos
  (dicas, `robots.txt`, caminhos usuais, links da home), mede o que cada um publica agora (itens
  das últimas 48 h, com título e data), descarta arquivo, lista defasada e HTML, pontua com regras
  explícitas e grava um principal e uma reserva de outro tipo. O CSV de levantamento é só dica: é
  uma fotografia de uma data e de uma rede. Fatia 1 (sondar e gravar) e fatia 2 (o `discover` usa o
  principal e a reserva, com a cascata de sempre de plano B) e fatia 3 (reavaliação
  automática por saúde, idade e espera crescente; modelos para URLs com data) feitas. O `run`
  reavalia só as fontes na vez antes de cada descoberta (`probe-sources --due`). Cobertura do site
  (seções, união das fontes, complementos) e planilha de conferência (`export-strategies`) feitas;
  a descoberta ainda não lê o conjunto principal + complementos. Correções à mão por fonte
  (`estrategias_manuais.csv`: excluir ou forçar um endereço) e regras vindas da conferência de sites
  reais (listagens de tags fora, nova tentativa para o que o site declara, índices de várias
  famílias, índices de feeds) feitas. Para a fonte sem sitemap nem feed a estratégia é a varredura
  de listagens (home e páginas de seção, com novas seções achadas nas seções; sem datas, nota mais
  baixa); a descoberta ainda não a lê.
- **Um critério, em um lugar** (`criteria.py`): a matéria entra se a análise **leu o texto** e achou
  **entidade identificável**. Evidência e corpus olham só para ela (a regra existe em Python e em
  SQL, com teste de concordância).
- **Um comando, um agendador:** `run` encadeia as etapas (cada uma é um subcomando, retomável pelo
  estado); o cron chama `scripts/run.sh`. Uma etapa que falha não impede as seguintes.
- **HTTP direto primeiro:** o HTML baixado já é a evidência; Bright Data (pago) só como fallback e
  só para fato adverso.
- **Portas e adaptadores:** cada etapa é uma interface; a escrita no corpus (`CorpusWriter`) grava
  direto no Postgres (sem a fila do KyX, cujo consumer exige um `RunContext`).
- **HTTP síncrono + threads:** o trabalho é só I/O e cada host é atendido uma requisição por vez
  (`ThrottledFetcher`, trava por host); evita a complexidade de `asyncio` na POC.
- **Leitura defensiva de XML:** sitemaps são conteúdo de terceiros — sem entidades externas, sem
  rede, e limite de 50 MB descomprimido (gzip bomb).
- **Estado em Postgres** (`midias_crawler_*`, o mesmo banco do corpus): URLs vistas, saúde por
  site, análises, evidências e gravações. Começou em SQLite e migrou em 2026-10-16 (o DDL é a fonte
  do esquema e vira migration do powerdd-backend).
- **Backfill histórico:** fora do escopo inicial; pensado depois, com janela finita.

## Fases

### Prioridade atual: mapa de cobertura antes da coleta ampla

Os levantamentos em `var/estrategias.xlsx` e nas planilhas auxiliares são a base existente.
Percentual sobre a união de candidatos recentes não comprova cobertura das seções do site.
O trabalho segue esta ordem:

1. Inventário persistente, completo e retomável das páginas candidatas a seção: primeira
   implementação em `map-sites`, com checkpoint por página, origem, fila e falhas explícitas
   (`004_create_midias_crawler_section_pages.sql`). Exporta catálogo, páginas, mecanismos
   encontrados e amostras sem truncar o inventário. A estratégia anterior aparece como referência.
   A aba Revisão distingue falhas das entradas: proibição explícita de robots, falha ao ler a
   política, limites, DNS, TLS, rede, conteúdo e redirecionamento fora do escopo. Preserva o estado
   e o motivo e não dispara tentativas ou mudanças no catálogo automaticamente.
2. Ampliar o reconhecimento de seções pela navegação e cruzar a estrutura com sitemaps; distinguir
   editorias, taxonomias e serviços. A heurística de URL atual ainda é limitada. Primeira
   classificação por evidência implementada (DDL 006): schemas da própria página, com proteção
   contra artigos destacados de outras URLs e metadados conflitantes; tipo desconhecido fica
   para revisão. Registra navegação explícita, que passa à frente dos palpites de URL na fila.
   Matérias e serviços reconhecidos não expandem a fila. A navegação também permite reconhecer
   rotas de seção com extensão HTML/PHP, antes ignoradas.
   Diretórios de tópicos (`topic`, `topics`, `topics-list`, `themen`, `tagg`, `amnen`) são taxonomias e ficam
   preservados para revisão, inclusive quando já estavam pendentes antes dessa classificação.
   A entrada cadastrada mantém prioridade e não é excluída por esse caminho.
   A expansão relativa observada em `exit.al/category/category/category/...` é suspensa como
   possível ciclo, mantendo os registros para revisão. Não impõe teto geral à profundidade.
   O modo contínuo também cruza caminhos de matérias em mecanismos lidos com o inventário:
   prefixos ainda desconhecidos entram como candidatas `mechanism_path`, respeitando escopo,
   taxonomias, serviços e datas; seguem sem confirmação até leitura. A exploração e a auditoria
   se repetem quando esses caminhos revelam páginas ou mecanismos novos.
3. Validar e associar sitemap/feed/listagem a cada seção; comparar as matérias da seção com os
   mecanismos encontrados. Primeira conferência de amostras implementada em `--validate-sources`:
   lê mecanismos com orçamento incluindo robots/filhos, guarda as URLs em tabela separada (005)
   e exporta correspondências e lacunas. Não declara cobertura total a partir da amostra.
   Paginação HTML em `/index/feed/pagina-N` não é índice de feeds: não expande o histórico,
   mesmo que tenha sido classificada como índice em uma execução anterior. A correção preserva
   URLs observadas, custos e datas; XML válido nesse mesmo padrão continua sendo avaliado.
   Links a comentários são identificados antes de remover fragmentos para comparar URLs:
   feeds só de comentários ficam inválidos para cobertura, e feeds mistos não contabilizam
   esses links. As observações originais permanecem disponíveis para revisão.
   A aba Complementos sugere mecanismos pelo ganho incremental nos links observados por seção,
   preservando datas, leituras parciais e lacunas. Reutiliza um índice de URLs canônicas por fonte
   no relatório. A seleção é heurística e não muda a estratégia de coleta automaticamente.
   Manter seções sem publicação recente no inventário.
4. Conferir um piloto de estruturas diferentes e depois ampliar aos 1.294 registros, em lotes.
5. Fazer a descoberta consumir o conjunto validado, deduplicando as matérias.

O inventário e a coleta são comandos independentes. `ok` numa página significa leitura, não
cobertura validada. Esgotar o orçamento deixa fila pendente; esgotar a fila reconhecida também
não autoriza afirmar que todas as seções do site foram descobertas. A implementação inicial
não muda o cron nem inicia coleta/enriquecimento/corpus.

| Fase | Entrega | Critério de aceite |
|---|---|---|
| 0. Esqueleto | Projeto, catálogo de fontes | pytest/ruff/mypy verdes; 1294 únicas |
| 1. Viabilidade | Parser de sitemap + relatório por site | Relatório dos 1294; amostra de 20–30 |
| 2. Descoberta incremental | Estado + corpus + backlog + filtro L0 + leitura parcial + cascata sitemap/feed/Bright Data (**feito**); medir volume e latência reais com as rodadas agendadas (pendente) | 2ª rodada sem URLs repetidas |
| 3. Triagem | ~~Triagem por manchete~~ (**removida** em 2026-10-16: ver Decisões); fica o filtro L0 | — |
| 4. Extração, análise, evidência | Texto por HTTP direto, análise de contexto (OpenRouter) e evidência com reaproveitamento validado do HTML (**feito**, armazenamento local); gravação direta no corpus Postgres (**feita**, `corpus-sync`, testada contra o banco local); S3 só com cliente falso | Compatível com `midias_ia_article*` |
| 5. Avaliação | Rotulagem humana ("entra no corpus?") e comparação com a busca ao vivo | Precisão e recall medidos; cobertura e custo por artigo |

## Estrutura

```
src/buscador_midias/
  domain/      modelos puros
  sites/       catálogo de fontes
  net/         fetcher educado (limite de tamanho, intervalo por host)  [fase 1]
  discovery/   robots → sitemap/feed (cascata) → relatório, filtro L0
  strategy/    sondar candidatos, medir, pontuar e escolher a estratégia de cada fonte
  criteria.py  o critério do corpus (Python e SQL)
  extraction/  artigo: texto, data, idioma                    [fase 4]
  analysis/    cliente OpenRouter, entidades, temas, resumo   [fase 4]
  evidence/    porta de armazenamento                         [fase 4]
  corpus/      CorpusWriter (Postgres), mapeamento, sync      [fase 4]
  state/       Postgres (midias_crawler_*) e migração do SQLite antigo  [fase 2]
```

## Regras de trabalho

TDD, SOLID, tipagem estrita (mypy strict), docstrings em todos os métodos públicos, testes sem
rede. Respeitar robots.txt e limitar a 1–2 requisições simultâneas por domínio.
