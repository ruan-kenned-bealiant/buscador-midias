# Buscador de mídias

Busca contínua de notícias em feeds e sitemaps de fontes confiáveis. Acha o que foi publicado no
dia, analisa se interessa (entidades, fato adverso, categoria), captura a evidência e grava no
corpus de Mídias IA, que alimenta o KyX e o PowerDD. É independente deles: só compartilha o banco
`power_dd_midias` e o corpus.

## Como funciona

A notícia é uma linha em `bm_articles` que atravessa seis etapas (`discovery`, `dedup`, `fetch`,
`context`, `evidence`, `persist`). Uma fila RabbitMQ por etapa avisa os workers; o banco é a verdade.
Rodadas por horário (01h, 06h, 12h e 16h de Brasília). A saúde das fontes roda à parte.

```
scripts/bm.sh db install                       # tabelas bm_*
scripts/bm.sh dashboard                         # painel em http://127.0.0.1:8765
scripts/bm.sh pipeline serve --role worker --stage fetch
scripts/bm.sh pipeline queues                   # filas e consumidores
```

Em produção cada papel é um serviço systemd (`deploy/systemd/`).

## Documentação

Comece por [docs/atual/README.md](docs/atual/README.md): arquitetura, dados, fluxo, saúde das fontes,
mecanismos, API e operação.

## Testes

```
BM_TEST_POSTGRES=1 BM_TEST_RABBIT=1 .venv/bin/python -m pytest
```
