# Arquitetura

O buscador lê feeds e sitemaps de fontes confiáveis, acha as notícias do dia, analisa o que
interessa e grava no corpus de Mídias IA. Ele é **independente do KyX e do PowerDD**: usa o
mesmo banco (`power_dd_midias`, schema `public`), mas só se encontra com eles no corpus
(`midias_ia_articles` e tabelas `midias_ia_*`), que ele alimenta, e na lista de fontes
(`midias_ia_trusted_sources`), da qual é o dono.

## Ideia central

A **notícia é o centro**: uma linha em `bm_articles` guarda onde ela está, o que aconteceu, o custo
e o motivo da falha. O banco é a verdade; o RabbitMQ só avisa "há trabalho nesta etapa" com a
mensagem `{"s": etapa, "k": chave}`. Perder uma mensagem nunca perde trabalho: o `maintain`
republica o que ficou parado.

```
 scheduler ──cria rodada──▶ bm_runs / bm_run_links
     │                          │
     │                    bm_discovery_tasks ──▶ bm_articles (uma por URL)
     ▼                                                │
 publisher ──{etapa,chave}──▶ RabbitMQ bm.<etapa>[.recovery]
                                   │
        worker@discovery · dedup · fetch · context · evidence · persist
                                   │  (claim por compare-and-set, ack só após o checkpoint)
                                   ▼
                       corpus (midias_ia_articles) + evidência (S3 ou disco)
```

## Processos (um por papel, escaláveis)

| Unidade systemd | O que faz |
|---|---|
| `buscador-midias-scheduler` | Cria a rodada no horário, recupera reservas vencidas, republica mensagens perdidas, fecha leituras e rodadas, grava as estatísticas. |
| `buscador-midias-publisher` | Escolhe o que publicar em cada fila: justiça por host, vagas por host, contrapressão. |
| `buscador-midias-window` | Fecha, sem HTTP, as notícias que a data já exclui. |
| `buscador-midias-worker@<etapa>` | Consome uma etapa. Dentro dele há threads só para a concorrência de rede. |
| `buscador-midias-health` | Rodadas de saúde das fontes (inativa, exclui, recupera, troca o transporte). Um líder por banco. |
| `buscador-midias-painel` | Painel e API local (`BM_BACKEND=bm`). |

A profundidade das filas e o número de consumidores no RabbitMQ são a medida de gargalo: o painel
mostra por etapa e avisa quando há trabalho esperando sem worker.

## Pacotes (`src/buscador_midias`)

| Pacote | Conteúdo |
|---|---|
| `db/` | Esquema `bm_*`, importador, repositórios (cadastro, configurações, fila, rodadas, saúde, leitura, painel), `WorkStore`. |
| `pipeline/` | `broker.py` (RabbitMQ) e `service.py` (papéis, `serve`). |
| `ingestion/` | O motor das seis etapas (`engine.py`), regras puras (`flow.py`), filtro de conteúdo, rotas, memo de documentos. |
| `catalog/` | Mecanismos (RSS, sitemap, G1…), transportes HTTP, regras de saúde, sonda de links. |
| `analysis/`, `extraction/`, `evidence/`, `corpus/`, `net/`, `domain/` | Análise por IA, extração de texto, captura de evidência, escrita no corpus, rede e tipos. |
| `dashboard/` | Servidor local, API e as telas (Painel, Gerenciador, Mecanismos, Saúde, Buscador, Guia). |

## Limites de conexão

O banco aceita 100 conexões e é compartilhado. Descoberta e extração seguram uma conexão própria por
leitura em andamento (o lock de host), então os pools são pequenos (`pool_size` em
`pipeline/service.py`; `BM_POOL_MAX` sobrescreve). Antes de aumentar o número de workers, some:
pools + leituras simultâneas + o que o KyX/PowerDD usam.
