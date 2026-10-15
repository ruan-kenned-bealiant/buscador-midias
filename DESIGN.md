# Design

## Objetivo

Substituir a busca ao vivo na web por uma coleta proativa: varrer sitemaps, triar, analisar,
capturar evidência e alimentar o corpus (`midias_ia_*`, Postgres `pgsql_midias`) e o bucket.

## Decisões

- **Descoberta só por sitemap** (news sitemap traz título e data sem baixar o artigo).
- **O corpus é por entidade, não por adversidade:** entra toda matéria com ao menos uma pessoa ou
  organização identificável (adversa ou não); só se descarta o que não tem quem identificar. O fato
  adverso é uma marca (`finding`, temas), nunca um filtro. O veredito por entidade e tema
  (`match_nome`, `alerta`) continua no Treatment, na hora da DD, e a matéria sem tema adverso é
  achada no KyX pela categoria "Diversos" (palavra-chave `diversos`).
- **Triagem em camadas:** L0 URL/seção, L1 título, L2 título + lede (LLM). Descartar só com alta
  confiança e auditar 1–2% do descartado.
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
| 2. Descoberta incremental | Estado + corpus + backlog + filtro L0 + leitura parcial (**feito**); medir volume e latência reais com rodadas espaçadas (pendente) | 2ª rodada sem URLs repetidas |
| 3. Triagem | Triagem por manchete com LLM (**feita**, `permissive`, multilíngue, custo no relatório); L1 Naive Bayes opcional; segunda passada com o texto e rótulo humano pendentes | Precisão/recall medidos |
| 4. Extração, análise, evidência | Texto por HTTP direto, análise de contexto (OpenRouter) e evidência com reaproveitamento validado do HTML (**feito**, armazenamento local); gravação direta no corpus Postgres (**feita**, `corpus-sync`, testada contra o banco local); S3 só com cliente falso | Compatível com `midias_ia_article*` |
| 5. Avaliação | Rotulagem humana ("entra no corpus?") e comparação com a busca ao vivo | Precisão e recall medidos; cobertura e custo por artigo |

## Estrutura

```
src/buscador_midias/
  domain/      modelos puros
  sites/       catálogo de fontes
  net/         fetcher educado (limite de tamanho, intervalo por host)  [fase 1]
  discovery/   robots → sitemap → sondagem/relatório          [fase 1]
  triage/      pré-avaliação em camadas (L0 pronto)           [fase 2-3]
  extraction/  artigo: texto, data, idioma                    [fase 4]
  analysis/    entidades, temas, resumo                       [fase 4]
  evidence/    porta de armazenamento                         [fase 4]
  corpus/      CorpusWriter (Postgres), mapeamento, sync      [fase 4]
  state/       SQLite                                         [fase 2]
```

## Regras de trabalho

TDD, SOLID, tipagem estrita (mypy strict), docstrings em todos os métodos públicos, testes sem
rede. Respeitar robots.txt e limitar a 1–2 requisições simultâneas por domínio.
