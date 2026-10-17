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

### Prioridade atual: mapa de cobertura antes da coleta ampla

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
