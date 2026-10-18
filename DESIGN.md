# Design

## Objetivo

Consolidar o mapa estrutural das 1.294 fontes, distinguindo editorias e subseções
confirmadas, candidatas, relações registradas e lacunas. A entrega atual termina com um
inventário final e suas limitações, sem retomada automática.

## Consulta do mapa

O painel abre a tela de mapeamento em `http://127.0.0.1:8765`:

```bash
scripts/bm.sh dashboard
# Catálogo alternativo: scripts/bm.sh dashboard --sites caminho/sites.csv
```

A tela mostra os **1299 registros do catálogo**: 1294 fontes
únicas e 5 duplicatas identificadas. Ao selecionar uma fonte, aparecem suas
páginas/seções mapeadas, evidência do tipo, última leitura e um link de notícia de exemplo
quando armazenado. Há busca por fonte/domínio/país, filtros, busca por seção e paginação.
O modo inicial mostra páginas lidas com exemplos; os demais permitem ver todas as lidas,
pendências/falhas e todo o mapa estrutural. Fontes sem seções ou sem exemplos continuam no catálogo.
Um exemplo é um link observado na leitura da página; não foi revalidado ao abrir o painel.
Não existe uma referência conhecida para medir 100% de cobertura do site.

As rotas `/` e `/mapa` servem esta tela; `/fluxo` preserva o painel operacional anterior.
`/api/mapa/fontes` consulta o catálogo e `/api/mapa/secoes?site=CHAVE` fornece as seções paginadas.
Essas consultas abrem o estado somente leitura e não disparam buscas nos sites.

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

## Evolução futura: manutenção periódica do mapa

Planejada, sem agendamento ou implementação nesta entrega. Uma execução periódica terá
orçamento próprio de chamadas, tempo, páginas e profundidade. Começa pelas entradas e menus
conhecidos, compara o retrato anterior e prioriza caminhos novos ou alterados.

- Usa URLs normalizadas, histórico de visitas e proteção contra ciclos; cada execução termina
  pelo orçamento ou pela seleção fechada, mesmo se houver novos caminhos a examinar.
- Separa editorias/subseções de artigos, serviços e taxonomias; respeita escopo e robots.
- Registra novos caminhos como candidatas, com origem e evidência; confirma o tipo por leitura.
- Preserva falhas com espera antes de outra tentativa; ausência em um menu não prova remoção
  global da seção e não elimina evidências antigas automaticamente.
- Entrega diferenças observadas: caminhos novos, confirmados, alterados e pendentes. Não calcula
  um percentual de cobertura total sem uma referência externa confiável do próprio site.

Essa manutenção é independente do fechamento do inventário atual e não reabre sua etapa
indefinidamente nem implica coleta contínua de notícias.

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
  a descoberta lê principal/reserva + complementos, com deduplicação, checkpoints e orçamento
  compartilhado para as chamadas gratuitas. Correções à mão por fonte
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

### Histórico do levantamento de cobertura

Os levantamentos em `var/estrategias.xlsx` e nas planilhas auxiliares são a base existente.
Percentual sobre a união de candidatos recentes não comprova cobertura das seções do site.
O trabalho segue esta ordem:

A varredura aberta foi encerrada em 2026-10-17 por decisão do usuário. A prioridade passou
a ser consolidar os checkpoints e complementar lacunas de cobertura, mantendo caminhos não
explorados e restrições explícitos. O encerramento não significa que todas as seções foram
descobertas. As próximas execuções têm orçamento total por fonte e prazo global, além dos
limites por lote: 200 chamadas à pilha HTTP e 180 segundos por fonte, 1800 segundos para a
exploração do conjunto, configuráveis. Páginas, robots e mecanismos compartilham o saldo.
O prazo é conferido antes da próxima chamada; requisições em andamento usam os timeouts
existentes, e exportação fica à parte. O inventário permanece retomável ao atingir o teto.
`--report-only --priorities-only` classifica as fontes para complementação: ausência de
mecanismo recente confirmado, lacunas nas amostras, acesso/estrutura a revisar e amostras
correspondentes. Isso não altera a estratégia de coleta nem inicia novas buscas.

O piloto dirigido posterior conferiu 20 fontes em 257 segundos/523 chamadas: 15 tiveram
ganho e 16.508 URLs distintas das mesmas amostras ganharam correspondência, sem perda nas
anteriores. Foram encontrados 42 mecanismos recentes novos; 68 endereços recentes, incluindo
reavaliações, ficaram como dicas reutilizáveis. As prioridades globais passaram a 152/818/280/44.
A descoberta isolada em cinco fontes validou união e deduplicação. A seleção orientada a
sitemaps de notícias/feeds datados acrescentou 345 candidatas com publicação declarada nas
últimas 48 horas, além dos principais escolhidos para o piloto. Índices amplos também
acrescentaram URLs, mas trouxeram muito arquivo e atingiram o orçamento em uma segunda leitura.
O ganho deve ser medido com recência e tipo de data, mantendo as limitações de amostra e
orçamento: `lastmod` não comprova publicação e URLs aceitas por L0 não são artigos confirmados.
Resultados: `var/piloto-resultados.xlsx` e `var/piloto-validacao-final.json`.
Os pilotos isolados não alteraram estratégias, estado incremental, corpus ou cron e não
utilizaram serviços pagos. A integração automática foi implementada depois: lê complementos
gravados e o plano validado das cinco fontes em `data/plano_descoberta.json`, sem reabrir
o inventário. O principal usa a reserva somente se falhar; complementos acrescentam URLs
mesmo se ele servir. A união canônica preserva publicação declarada e checkpoints dos filhos;
falhas parciais não descartam os demais resultados. O padrão limita o conjunto a três
complementos, 40 chamadas gratuitas e 120 segundos para iniciar chamadas por fonte, incluindo
robots e fallback gratuito (redirecionamentos/retentativas internos ficam à parte). Os
limites existentes por mecanismo continuam valendo. A origem `combined` exige SQL 007;
sem essa migração, o comando para antes do HTTP. Regras manuais, escopo e robots se aplicam
também aos complementos. Catálogos personalizados exigem plano explícito; `--no-strategy`
desliga as escolhas e o plano, e `--max-complements 0` desliga a união.

A validação da integração pelo CLI real usou apenas HTTP gratuito e estado incremental em
um schema descartável do banco de testes: cinco fontes, duas leituras, todas sem falhas ou
leituras parciais. A primeira reuniu 6.053 URLs candidatas, 2.425 adicionais dos complementos;
352 dessas adicionais tinham publicação declarada nas últimas 48 horas. A segunda registrou
zero URLs novas e pulou dois filhos com checkpoint inalterado. Foram 68 chamadas HTTP de
transporte. Isso valida a integração e a deduplicação, sem comprovar cobertura total ou
relevância das notícias. Relatórios: `var/integracao-complementos-piloto.xlsx` e
`var/integracao-complementos-piloto.json`. A migração 007 foi aplicada no Postgres local,
preservando os 180 registros de rodadas existentes; o corpus e o cron não foram alterados.


O lote dirigido seguinte conferiu outras 20 fontes em 125,7 segundos/314 chamadas,
com 231 leituras/reavaliações e 31 mecanismos novos recentes. Sete fontes passaram a ter
mecanismo recente confirmado (42 endereços exportados). Oito fontes tiveram ganho nas
mesmas amostras congeladas: 2.288 URLs únicas por fonte, sem perda das correspondências
anteriores. As prioridades globais ficaram em 145/825/280/44. Treze fontes permanecem para revisão; ColombiaCheck ganhou correspondência,
mas continuou sem confirmação de mecanismo recente. O lote não explorou novas seções nem
alterou estratégias de coleta, estado incremental, corpus ou cron. Os mecanismos exportados
seguem como dicas para validação incremental, sem promoção automática ao plano. Resultados:
`var/lote-02/resultados.xlsx` e `var/lote-02/validacao-final.json`. Outro lote foi apenas
selecionado em `var/lote-02/proximas-fontes.txt`, sem execução.

1. Inventário persistente, completo e retomável das páginas candidatas a seção: primeira
   implementação em `map-sites`, com checkpoint por página, origem, fila e falhas explícitas
   (`004_create_midias_crawler_section_pages.sql`). Exporta catálogo, páginas, mecanismos
   encontrados e amostras sem truncar o inventário. A estratégia anterior aparece como referência.
   A aba Revisão distingue falhas das entradas: proibição explícita de robots, falha ao ler a
   política, limites, DNS, TLS, rede, conteúdo e redirecionamento fora do escopo. Preserva o estado
   e o motivo e não dispara tentativas ou mudanças no catálogo automaticamente.
   Três falhas consecutivas de conexão na mesma origem, dentro do mesmo lote de páginas
   ou passagem de auditoria, suspendem somente aquela origem, mantendo suas páginas/mecanismos
   não tentados pendentes e o motivo explícito. Demais hosts continuam sendo mapeados e
   auditados, inclusive prefixos novos apontados pelos mecanismos. A suspensão é mantida entre
   lotes e passagens da mesma execução; outra execução pode retomar a origem.
2. Ampliar o reconhecimento de seções pela navegação e cruzar a estrutura com sitemaps; distinguir
   editorias, taxonomias e serviços. A heurística de URL atual ainda é limitada. Primeira
   classificação por evidência implementada (DDL 006): schemas da própria página, com proteção
   contra artigos destacados de outras URLs e metadados conflitantes; tipo desconhecido fica
   para revisão. Registra navegação explícita, que passa à frente dos palpites de URL na fila.
   Matérias e serviços reconhecidos não expandem a fila. A navegação também permite reconhecer
   rotas de seção com extensão HTML/PHP, antes ignoradas.
   Diretórios de tópicos/pessoas (`topic`, `topics`, `topics-list`, `themen`, `tagg`, `amnen`,
   `persons`) são taxonomias e ficam
   preservados para revisão, inclusive quando já estavam pendentes antes dessa classificação.
   A entrada cadastrada mantém prioridade e não é excluída por esse caminho.
   A expansão relativa com três segmentos iguais consecutivos, observada em `exit.al`
   (`category/category/category/...`) e `monde-diplomatique.fr` (`index/index/index/...`),
   é suspensa como possível ciclo, mantendo registros para revisão, sem teto geral de profundidade.
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
5. **Implementado:** a descoberta consome o conjunto validado, deduplica as candidatas e
   conserva as regras incrementais, os checkpoints e os limites por fonte. A ampliação para
   novas fontes permanece dirigida pelas prioridades; não há varredura aberta automática.

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
