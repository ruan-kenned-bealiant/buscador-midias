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
- **Estado da POC em SQLite** (URLs vistas, saúde por site), descartável.
- **Backfill histórico:** fora do escopo inicial; pensado depois, com janela finita.

## Fases

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
  criteria.py  o critério do corpus (Python e SQL)
  extraction/  artigo: texto, data, idioma                    [fase 4]
  analysis/    cliente OpenRouter, entidades, temas, resumo   [fase 4]
  evidence/    porta de armazenamento                         [fase 4]
  corpus/      CorpusWriter (Postgres), mapeamento, sync      [fase 4]
  state/       SQLite                                         [fase 2]
```

## Regras de trabalho

TDD, SOLID, tipagem estrita (mypy strict), docstrings em todos os métodos públicos, testes sem
rede. Respeitar robots.txt e limitar a 1–2 requisições simultâneas por domínio.
