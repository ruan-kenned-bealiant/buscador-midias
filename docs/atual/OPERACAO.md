# Operação e desenvolvimento

Os comandos desta página são referência para o operador. Esta documentação foi produzida sem executá-los contra a rodada em andamento. Diferencie consultas, inicialização de esquema e operações que geram trabalho/custo.

## Ambiente

Python **3.12+**, PostgreSQL, RabbitMQ e acesso aos sites/ao OpenRouter são necessários para o fluxo completo. O painel é Python/HTML/JavaScript; não requer executar o frontend PowerDD. Dependências e extras estão em [pyproject.toml](../../pyproject.toml).

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,xlsx]'
# Apenas se for armazenar evidência em S3:
.venv/bin/python -m pip install -e '.[s3]'
```

`curl_cffi` é dependência do produto. Playwright está no extra experimental `access-pilot`; não é necessário para a coleta HTTP normal. Não se deve instalar um navegador para tentar corrigir todo erro de XML.

## Configuração e segredos

| Variáveis / arquivo | Uso |
|---|---|
| `DB_MIDIAS_IA_HOST`, `PORT`, `DATABASE`, `USERNAME`, `PASSWORD` com esse prefixo | Conexão PostgreSQL de Mídias |
| `KYX_ENV` | Arquivo de credenciais lido por `scripts/bm.sh`; padrão `.env` do KyX vizinho |
| `CORPUS_DB_HOST` | Host do banco no wrapper; padrão local `127.0.0.1` |
| `PY` | Interpretador utilizado pelo wrapper; padrão `.venv/bin/python` do projeto |
| `BM_CATALOG_BACKEND`, `BM_CATALOG_SCHEMA` | Seleção explícita do catálogo; instalação atual usa PostgreSQL e `midias_manager` |
| `var/gerenciador/storage.json` | Seleção persistida no corte; lida por `catalog/storage.py` |
| `RABBITMQ_HOST`, `PORT`, `USER`, `PASSWORD`, `VHOST` com esse prefixo | Broker exclusivo da ingestão |
| `BM_QUEUE_PREFIX` | Prefixo das filas; padrão de código `midias.ingest` |
| `BM_INGEST_WORK_DIR` | Diretório compartilhado de artefatos; altera também a base usada para evidência local pelo engine |
| `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` | Contexto; modelo é configuração da ingestão, não o nome da variável de chave |
| `MIDIAS_IA_STORAGE_DISK` | `local` ou `s3` |
| `AWS_BUCKET`, `AWS_ENDPOINT`, `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_USE_PATH_STYLE_ENDPOINT` | Armazenamento S3/compatível |
| `BRIGHTDATA_API_KEY`, `BRIGHTDATA_ZONE_UNLOCKER`, `BRIGHTDATA_BASE_URL` | Suporte opcional de testes/saúde e caminhos legados; ingestão atual não habilita fallback |
| `BUSCADOR_STATE_SCHEMA` | Esquema do estado legado; não seleciona o catálogo novo |

`bm.sh` lê apenas chaves permitidas, sem `eval`. Valores já exportados prevalecem sobre o arquivo lido. Para o subcomando `ingest`, também carrega chaves conhecidas de `var/ingestion/rabbit.env`. O wrapper define o host de banco por `CORPUS_DB_HOST`; não basta editar apenas outra variável de host no `.env` e esperar que esse wrapper a preserve.

O padrão cru de `broker.py` usa porta 5672/usuário guest, mas o Compose exclusivo publica AMQP em **127.0.0.1:5673** e gestão em **127.0.0.1:15673**. Configure o ambiente correspondente. Credenciais reais não devem aparecer em exemplos, logs, documentação ou URLs.

## Preparação da base e inicialização

A instalação atual já possui catálogo importado. Para uma instalação nova baseada no retrato SQLite existente, siga a [migração PostgreSQL](../MIGRACAO_POSTGRES_GERENCIADOR.md): o comando `migrate-catalog ... --activate` cria/importa/confere o esquema e registra a seleção. Ele não é um comando de sincronização diária. Não reimportar o SQLite antigo sobre edições novas do PostgreSQL.

O writer requer o corpus `midias_ia_*` preexistente e compatível; ele verifica colunas, não instala todo o banco de Mídias. SQLs de classificação/transporte são aditivos e devem acompanhar a versão instalada. O catálogo sem configuração explícita ainda tem suporte SQLite; saúde e ingestão exigem PostgreSQL. Ausência de configuração não equivale a migração automática.

Com catálogo e corpus preparados:

```bash
scripts/bm.sh ingest init
scripts/bm.sh dashboard --host 127.0.0.1 --port 8765
# Em processo próprio, quando for operar a ingestão:
scripts/bm.sh ingest serve
```

`ingest init` cria/ajusta tabelas aditivas e completa chaves de configuração ausentes, preservando valores salvos. Até `ingest status` inicializa estruturas se necessário; não o trate como consulta SQL estritamente sem escrita em ambiente não preparado. O agendamento nasce desabilitado no código, mas uma instalação existente conserva suas configurações.

O painel inicia o worker de saúde e suas configurações/DDL. Habilitar uma rotina ou solicitar rodada faz requisições externas. Na busca, OpenRouter tem custo; saúde/testes podem usar Bright Data conforme opção/configuração. No teste interativo, omitir a opção usa a disponibilidade das credenciais como padrão: envie `bright_data=false` para garantir que não será usado. A chamada HTTP de descoberta não chama IA por si só.

## RabbitMQ e serviço

O [Compose](../../deploy/rabbitmq.compose.yml) usa volume externo `buscador_midias_rabbit_data`; ele deve existir antes do primeiro `up`. Usa portas locais e reinício `unless-stopped`. Não executar `down -v` ou remover volume para reduzir filas: isso elimina estado do broker.

```bash
# Primeira preparação do broker, com variáveis privadas já configuradas:
docker volume create buscador_midias_rabbit_data
docker compose --env-file var/ingestion/rabbit.env -f deploy/rabbitmq.compose.yml up -d
```

A unidade de exemplo [buscador-midias-ingest.service](../../deploy/systemd/buscador-midias-ingest.service) executa `scripts/bm.sh ingest serve`, com reinício em falha e desligamento cooperativo. Seus caminhos absolutos são desta máquina: ajuste `WorkingDirectory`, `EnvironmentFile` e `ExecStart` em outra instalação. Copiar o arquivo para a configuração do serviço de usuário e habilitá-lo é uma ação de implantação, não ocorre apenas por existir no repositório.

O runtime pode separar papéis: `ingest serve --role scheduler|publisher|worker`, com `--stage` para workers de uma etapa. No código atual o publicador cria os publicadores de todas as etapas mesmo que `--stage` seja informado; não assumir que esse filtro se aplica a todos os papéis. `--role all` também inicia o filtro de janela.

## Configuração da busca

| Chave | Padrão do código / efeito |
|---|---|
| `enabled` | false; autoriza novas rodadas automáticas |
| `workers_enabled` | true; autoriza processamento das existentes |
| `schedule_mode`, `schedule_hours`, `timezone` | `clock`, `[1,6,12,16]`, `America/Sao_Paulo` |
| `window_mode` | `today`; alternativa `lookback` |
| `lookback_minutes` | 2880; utilizado no modo lookback |
| `daily_routes_only` | true; restringe caminhos de atualizações |
| `allow_undated` | false; não libera sem data no modo diário |
| `max_attempts`, `retry_minutes` | 3 e 1; falhas comuns e espera base |
| `model`, `analysis_text_chars` | `openai/gpt-4.1-mini`, 12000 |
| `workers` | discovery 2, dedup 1, fetch 2, context 1, evidence 1, persist 1; são padrões, não contagem atual garantida |
| `content_filter`, `blocked_content_categories` | `shadow`, horóscopo/clima/culinária/esportes; ver ressalva sobre filtro final em [Fluxo](FLUXO_NOTICIAS.md) |
| `bright_data` | false; habilitação é rejeitada pela API da ingestão |
| `evidence_paid_adverse_only` | true; compatibilidade da política do capturador, sem ativar provedor pago ausente |

Alterar concorrência acima dos slots abertos requer reinício controlado do serviço. Ao separar processos, o total real depende de quantas instâncias foram iniciadas; o painel mostra seus heartbeats. Não extrapole uma previsão de duração apenas multiplicando workers.

## Diagnóstico sem interromper a rodada

```bash
df -h .
ss -ltnp 'sport = :8765'
systemctl --user status buscador-midias-ingest.service --no-pager
journalctl --user -u buscador-midias-ingest.service -n 100 --no-pager
scripts/bm.sh ingest status
```

Consulte `/buscador` para a rodada/etapas/sites e `/saude` para as verificações. Porta ocupada ao iniciar `dashboard` pode indicar painel já aberto: verificar o processo antes de iniciar outro ou encerrá-lo. Não usar `kill -9` como primeira medida de diagnóstico.

| Sintoma | Verificação indicada |
|---|---|
| Rabbit mostra zero consumidores | Runtime usa `basic_get`; conferir heartbeat, tarefas e logs |
| Fila diminui e fim fica lento | Hosts restantes, cooldown, retentativas, backpressure e conteúdo de tarefas; não concluir que workers foram reduzidos |
| Descoberta para de produzir | Reserva de disco, backlog seguinte, host ocupado, configuração de workers e outbox |
| Contexto acumula | Credencial/modelo, limites/resposta do OpenRouter, cooldown e custo/uso registrado |
| 403/503 abre no navegador | Comparar transporte/headers/sessão, resposta e conteúdo; executar amostra finita pelo teste, sem presumir artigo válido |
| Site com 100% e pendências | Abrir tarefas e diagnósticos: falhas atuais/herdadas, cobertura parcial, data/evidência pendente |
| Evidência em branco | Conferir arquivo e resposta original, conteúdo realmente capturado, dependência de recursos externos e rejeições do capturador |
| PostgreSQL indisponível | Corrigir serviço/conexão; não há fallback automático para escrever SQLite |

Desligar apenas `enabled` impede novas rodadas automáticas e deixa as existentes escoarem. Desligar `workers_enabled` pausa processamento; manter o serviço vivo permite supervisão. Encerrar o serviço é outra ação. Retomada usa checkpoints/leases/outbox; não exige apagar filas nem reiniciar do zero. `.failed` requer diagnóstico e reprocessamento explícito, não purge.

## Evidências, disco e backup

HTML transitório pode crescer enquanto contexto/evidência aguardam; não remover esses arquivos de tarefas em aberto. Evidências definitivas devem ser preservadas junto com o corpus. O script abaixo só calcula o que poderia compactar quando executado sem `--apply`:

```bash
.venv/bin/python scripts/compact_old_evidence.py
```

A aplicação explícita compacta HTML com mais de 24 horas, verifica SHA-256 e mantém a chave lógica; registra ledger em `var/maintenance`. Não substitui uma política de backup. Ver [relatório da compactação](../LIMPEZA_ESPACO_20261026.md).

Backup deve abranger catálogo/saúde/ingestão, corpus e evidências, incluindo representações gzip e objetos S3 quando usados. O broker tem volume próprio; restaurar apenas Rabbit sem checkpoints do PostgreSQL não restaura todo o fluxo. Guardar configurações privadas separadamente e conferir vínculos/contagens após restauração. Relatórios/manifestos históricos externos são necessários para reproduzir auditorias e não estão todos convertidos em tabelas.

## Desenvolvimento e testes

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy
.venv/bin/pytest
```

Esses comandos são referência, não foram executados como parte desta alteração documental. **`pytest` não é estritamente offline nesta base:** `tests/conftest.py` tem fixture autouse que tenta conectar ao PostgreSQL local, criar o banco `buscador_midias_test` se necessário e criar/remover um esquema por teste. As credenciais podem vir do `.env` vizinho. O corpus de produção não deve ser apontado como destino dos testes.

Testes do catálogo/saúde/ingestão PostgreSQL usam habilitação explícita `BM_TEST_POSTGRES=1` e esquemas descartáveis. O teste real do Rabbit também exige `BM_TEST_RABBIT=1`; confira seu prefixo isolado. Sem dependências/configuração, vários testes são pulados: uma suíte verde com skips não comprova integração com infraestrutura.

As chamadas a sites/OpenRouter são substituídas por leitores/analisadores injetáveis nos testes de comportamento. Executar scripts de auditoria não equivale a rodar testes unitários: eles podem acessar internet, consumir serviços e aplicar alterações reais. Conferir `--help`, caminho de saída, manifesto e flags de aplicação antes de usar.

Coberturas importantes: canonicalização com fixture gerada pelo PHP, parser defensivo, escopo/DNS/TLS, amostras textuais, revisões concorrentes, recuperação de host/filas, janela diária, classificação/país, writer idempotente e leitura comprimida de evidência. O [inventário](INVENTARIO.md) localiza cada suíte.

## Manutenção da documentação

Ao alterar etapas/estados/defaults, revisar [Fluxo](FLUXO_NOTICIAS.md) e esta página; ao alterar DDL, revisar [Dados](DADOS.md); ao alterar endpoints, revisar [API](API.md); novos módulos/scripts devem entrar no [Inventário](INVENTARIO.md). Relatórios datados permanecem como observações da execução, sem substituir a especificação atual.

## Comandos da CLI e estágio de uso

| Comando | Finalidade / efeito |
|---|---|
| `dashboard` | Painel atual, com escrita de cadastro, saúde e comandos da busca; o texto antigo “somente leitura” no help não descreve todas as rotas atuais |
| `ingest init/status/serve` | Inicialização, diagnóstico e runtime das seis filas; entrada encaminhada pelo CLI ao módulo de ingestão |
| `ingest g1-pilot/sources-pilot` | Pilotos de coleta finita, com acesso externo; não são consultas de status |
| `migrate-catalog` | Importação transacional SQLite → PostgreSQL e conferência |
| `probe`, `probe-sources` | Sondagem de acesso e escolha de estratégias do levantamento anterior; fazem rede |
| `map-sites` | Mapeamento estrutural retomável; relatório sem nova rede somente com as opções correspondentes |
| `discover` | Descoberta incremental do fluxo anterior; grava estado |
| `enrich` | Texto/contexto/evidência do fluxo anterior; pode consumir provedores |
| `corpus-sync` | Escrita do fluxo anterior no corpus compartilhado |
| `run` | Encadeia o fluxo anterior; é chamado por `scripts/run.sh`/cron legado |
| `export-mechanisms`, `export-strategies` | Exportações do inventário/estratégias para artefatos; não configuram a ingestão atual |
| `strategy-health`, `report` | Diagnóstico do estado legado, distinto da tela `/saude` |
| `migrate-sqlite` | Migração do estado antigo, distinta de `migrate-catalog` |
| `labels export/serve/score` | Lote cego, tela de rotulagem e cálculo de precisão/recall do levantamento |

Consulte `python -m buscador_midias <comando> --help` antes de escolher opções. `ingest` é encaminhado antes do parser geral e não aparece na listagem principal do `--help`; seu contrato está em `ingestion/runtime.py`. A ajuda da CLI conserva descrições históricas, enquanto esta seção separa os caminhos operacionais.

## Limpeza de pilotos e testes

Filas de pilotos têm prefixos próprios (`midias.g1_pilot.*`, `midias.sources_pilot.*`,
`midias.daily_smoke.*`). Sua remoção deve usar o prefixo exato da execução encerrada,
inventário antes/depois e as duas condições `--if-empty --if-unused`. Contador de
consumidores zerado não basta, porque o runtime usa polling. Não aplicar essa limpeza
às filas `midias.daily.v1.*` da instalação atual. Filas antigas `midias.ingest.*` ainda
podem conter mensagens e não devem ser descartadas apenas pela idade do prefixo.

Arquivos `tests/**/test_*.py` são testes automatizados de regressão, diferentes de
artefatos de pilotos. O fato de uma auditoria ter acabado não torna automaticamente
seus testes redundantes: várias dessas rotinas continuam expostas na CLI ou na API.
A retirada de um subsistema legado deve remover em conjunto seu comando/rota, código,
testes específicos e referências documentais, preservando os parsers compartilhados.
Caches `__pycache__` podem ser recriados e não fazem parte do histórico.
