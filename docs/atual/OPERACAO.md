# Operação

## Ambiente

Python 3.12+, PostgreSQL (`power_dd_midias`), RabbitMQ e acesso à internet e ao OpenRouter.
`scripts/bm.sh <subcomando>` roda `python -m buscador_midias <subcomando>` com as credenciais do KyX
carregadas (só as variáveis que o buscador usa; o valor do `.env` nunca é executado). Para
`pipeline` e `dashboard` ele também entrega o broker de `var/ingestion/rabbit.env`, **sem** o
prefixo de filas do serviço antigo: as filas são `bm.<etapa>[.recovery]`.

```bash
python3.12 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'
scripts/bm.sh db install          # cria o que falta (repetir é seguro)
scripts/bm.sh db status           # contagens do cadastro
```

## Serviços

Os units estão em `deploy/systemd/` e são ligados com `systemctl --user link`.

```bash
for s in discovery dedup fetch context evidence persist; do
  systemctl --user start buscador-midias-worker@$s
done
systemctl --user start buscador-midias-{publisher,scheduler,window,health,painel}
scripts/bm.sh pipeline queues      # fila e consumidores de cada etapa
journalctl --user -u 'buscador-midias-*' -f
```

Parar o trabalho: `systemctl --user stop buscador-midias-{scheduler,publisher}` e depois os
workers (esperam até 150 s para terminar o que está em curso). Nada se perde: o estado está no banco
e o `maintain` recupera o que ficou reservado.

O painel fica em `http://127.0.0.1:8765` (`/`, `/buscador`, `/gerenciador`, `/mecanismos`,
`/saude`, `/guia`).

## Configuração

Tudo em `bm_settings` e editável no painel: horários, janela, tentativas, modelo de análise, filtros
(`content_filter`, `url_filter`, `lastmod_filter`, `head_filter`: `off`, `shadow` ou `enforce`),
categorias barradas, passadas por notícia que falha, e workers por etapa. Mudar o número de workers
de uma etapa exige reiniciar o `worker@<etapa>`.

## Banco e disco

- O banco aceita **100 conexões** e é compartilhado com o KyX/PowerDD. Cada worker usa um pool
  pequeno; descoberta e extração seguram uma conexão por leitura em curso. Se aparecer
  `too many clients`, reduza workers ou `BM_POOL_MAX`.
- Evidência vai para o S3 em produção (`MIDIAS_IA_STORAGE_DISK=s3`); em desenvolvimento, para
  `var/ingestion/`. O disco tem reserva: abaixo dela a descoberta e a extração pausam.
- Backups feitos no corte estão em `var/backups/` (cadastro anterior, arquivo de rodadas de 24/10 e
  as tabelas do crawler legado).

## Testes

```bash
BM_TEST_POSTGRES=1 .venv/bin/python -m pytest        # banco real, um schema por teste
BM_TEST_RABBIT=1 ...                                  # inclui os testes com RabbitMQ real
```
Os testes de banco criam e apagam um schema próprio. Os de RabbitMQ usam um prefixo descartável.
Rode a suíte sem rodada em andamento: ela disputa conexões com os serviços.

## Quando algo não anda

| Sintoma | Onde olhar |
|---|---|
| Etapa com fila e "sem worker" no painel | `systemctl --user status buscador-midias-worker@<etapa>` |
| Muitas pendências `blocked` num site | Tela Pendências; só Bright Data resolve 401/403 |
| Rodada que não fecha | Ver por etapa quem ainda tem `queued/running/retry`; `maintain` do scheduler republica e recupera |
| `too many clients` | Conexões acima; veja `pg_stat_activity` |
