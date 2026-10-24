# Operação do buscador de notícias

O serviço Python lê o catálogo PostgreSQL e entrega trabalho em seis filas duráveis:
`discovery`, `dedup`, `fetch`, `context`, `evidence` e `persist`, sob o prefixo
`midias.ingest`. Cada etapa também tem uma fila `.failed`. Mensagens contêm somente
o identificador da tarefa; resultados, tentativas e checkpoints ficam no esquema
`midias_manager` do banco de Mídias. A outbox confirma a entrega ao broker e permite
retomar após interrupções. Reentregas não criam outra tarefa para a mesma etapa e geração.

## RabbitMQ exclusivo

Container: `buscador-midias-rabbitmq`. Volume persistente:
`buscador_midias_rabbit_data`. AMQP local: `127.0.0.1:5673`.
Painel de administração: <http://127.0.0.1:15673>.
Usuário e vhost: `buscador`. A senha fica em `var/ingestion/rabbit.env`, com permissão
0600, fora do Git. O broker tem reinício automático e não compartilha filas ou
credenciais com KyX/PowerDD. O volume deve integrar os backups da instalação.

A definição reproduzível está em `deploy/rabbitmq.compose.yml`. Em uma instalação
nova, crie o volume com `docker volume create buscador_midias_rabbit_data`, gere
credenciais próprias no arquivo de ambiente e execute:

```sh
docker compose --env-file var/ingestion/rabbit.env -f deploy/rabbitmq.compose.yml up -d
```

Na instalação atual o container já foi criado diretamente pelo Docker. Não execute
o Compose sobre esse container sem antes planejar a substituição: o nome é o mesmo.
Preserve sempre o volume. Alterar `RABBITMQ_DEFAULT_PASS` não altera a senha de um
usuário já existente; use a administração do RabbitMQ e atualize o arquivo do serviço.

## Serviço e tela

Tela: <http://127.0.0.1:8765/buscador>. O painel HTTP e o serviço de ingestão são
processos separados. Fechar o navegador não interrompe a coleta.

```sh
scripts/bm.sh ingest init
scripts/bm.sh ingest status
scripts/bm.sh ingest serve
```

A unidade em `deploy/systemd/buscador-midias-ingest.service` executa o agendador,
o publicador e os workers. No usuário da instalação:

```sh
systemctl --user status buscador-midias-ingest.service
systemctl --user restart buscador-midias-ingest.service
journalctl --user -u buscador-midias-ingest.service -n 50
```

O agendamento nasce pausado. Ative-o na tela após o piloto e a verificação de
capacidade. Pausar agendamentos mantém tarefas existentes; pausar workers impede
o consumo delas. A janela inicial é 2880 minutos, independente da periodicidade
de cada link. Rodadas manuais aceitam datas fixas e IDs de links.

## Interpretação dos resultados

- XML acessível sem notícias na janela é uma consulta bem-sucedida.
- `known`: notícia completa já presente no corpus; não repete análise e captura.
- `outside_window`: publicação fora da janela congelada da rodada.
- `date_unknown`: data não confirmada; inclusão requer a opção explícita da tela.
- `no_entities`: texto analisado sem entidade identificável, fora do critério atual.
- `evidence_pending`: notícia gravada com evidência pendente; não é prova completa.
- `failed`: tentativas esgotadas. O detalhe preserva diagnóstico e permite reprocessar.
- `partial`: rodada encerrada com falhas, limites de profundidade ou pendências.

Ocorrências contam todos os links que trouxeram uma URL; notícias únicas contam
URLs canonicalizadas. Uma notícia pode ter várias origens e uma única análise.
Reprocessar uma falha usa a configuração atualmente salva, registra sua revisão
na auditoria e mantém a configuração original da rodada e as tentativas anteriores.
Falhas de acesso à conta/modelo OpenRouter adiam a fila de análise por cinco minutos,
sem gastar o orçamento de falhas da notícia nem reprovar a fonte. Uma resposta
recebida, mas inválida, usa as tentativas limitadas configuradas.
Custos OpenRouter vêm do retorno do provedor; chamadas Bright Data são contagens,
não valores faturados. O corpus usa as tabelas existentes de `public`; as tabelas
operacionais novas estão em `ingestion/schema.py`, para futura migration no PowerDD.

HTMLs temporários ficam compactados em `var/ingestion/work` até persistência ou
descarte sem entidades. Falhas preservam o artefato para reprocessamento. Evidências
usam o armazenamento configurado no ambiente. Descoberta aguarda quando há menos
de 128 MiB livres; isso exige intervenção de capacidade, não reprovação dos sites.

## Piloto de 24/10/2026

Rodada `a0ad4279956d4d8f9857f84da99cd0c0`, RSS de direitos humanos da Agência Brasil,
janela de 48 horas: 10 URLs únicas, 3 fora da janela e **7 notícias gravadas**.
Conferência direta no corpus confirmou texto, entidades e evidência em todas as 7.
Consumo registrado da análise: **US$ 0,0084612**, 15473 tokens de entrada e 1420
na saída; nenhuma chamada Bright Data. O modelo selecionado foi `openai/gpt-4.1-mini`,
que respondeu sob a política de retenção existente. A rejeição do modelo inicial
pelo OpenRouter e suas tentativas permanecem no histórico.

O serviço e o broker estão ativos, mas o agendamento integral permanece pausado:
a máquina tinha aproximadamente 337 MiB livres ao concluir o piloto. Antes de
ativar toda a base, disponibilize capacidade para o PostgreSQL, volume RabbitMQ e
artefatos/evidências. A reserva de 128 MiB do executor não substitui esse planejamento.

## Correções de vazão e uso do mapeamento — 24/10/2026

Cada etapa tem seu publicador RabbitMQ. Um acúmulo na descoberta/deduplicação não
bloqueia mensagens de análise, evidência ou gravação. O transporte mantém buffers
pequenos por fila; o restante continua durável na outbox PostgreSQL. Eventos de
trabalho concluído ou geração substituída recebem `discarded_at` e motivo, sem
ser apresentados como entregues ao broker e sem apagar o histórico da tarefa.

A deduplicação compara até 100 URLs por consulta ao corpus, incluindo hashes
legados e entidades. Reserva e conclusão usam transações por lote, com tentativas,
proteção de proprietário e idempotência individuais. A consulta não transfere o
corpo inteiro de artigos conhecidos.

Datas explícitas `pubDate`/`published`/Google News permitem excluir notícias fora
da janela antes de baixar a página. `updated`, `lastmod`, datas genéricas e pistas
legadas sem procedência não autorizam esse corte. A extração HTML continua
confirmando a publicação dos artigos que precisam ser lidos.

As rotas temporais são aprendidas a partir de publicações textuais comprovadas
nos testes do catálogo e nas novas extrações HTML. O perfil fica em
`ingest_route_profiles`, por fonte, com padrões e provas. Exige pelo menos três
notícias distintas em dois dias de partição; um contraexemplo invalida a família.
As regras são reavaliadas a cada cinco minutos. Sem prova suficiente, o caminho
continua elegível. O nome de um arquivo ou `lastmod` sozinho não exclui nada.

Em **Mecanismos de busca** há dois novos parâmetros:

- **Usar rotas temporais aprendidas:** 1 permite as inferências calibradas;
  0 desliga esse corte na coleta. Não modifica os testes de saúde.
- **Sobreposição das rotas temporais:** margem de 1 a 7 dias, padrão 2, antes e
  depois da partição. O histórico identifica a exclusão como inferida, sem atribuir
  uma data de publicação fictícia à notícia. Uma inferência não garante cobertura
  total; escolha 0 no parâmetro anterior para exigir leitura integral.

XMLs grandes são interpretados uma vez e geram planos comprimidos em
`ingest_document_plans`. Cada continuação emite até 500 candidatas e 64 filhos;
as URLs restantes permanecem no plano. A conclusão da última fatia apaga somente
o plano transitório, depois do commit, preservando tarefas, ocorrências e
resultados. Não é um limite de cobertura. Os filhos com sinais de atualização
ou partições recentes são priorizados. As regras podem dispensar partições antigas
já calibradas. O filtro da janela também atua no backlog existente, sem download
ou chamadas pagas para essas exclusões.

O controle de entrada aguarda quando a próxima etapa acumula trabalho: 5000 tarefas
na deduplicação, 2000 na extração e 200 em análise/evidência/gravação. Descoberta e
novos downloads também aguardam quando o disco cai abaixo de 256 MiB; a gravação
continua apta a liberar artefatos. Essa espera não cria falhas nem tentativas a cada
poucos segundos. O painel identifica essas filas como **Fluxo controlado** e
mostra o total de leituras evitadas.

O paralelismo atual é 2 descoberta, 1 deduplicação em lote, 8 extração, 2 análise,
2 evidência e 2 persistência. Continua editável na tela. Os limites por host foram
preservados. Novos agendamentos permanecem desligados por solicitação do usuário;
as 160 rodadas já existentes continuam sendo processadas.

Para recuperar espaço, os retratos SQLite anteriores ao corte PostgreSQL e os
backups de estado de 15–16/10 foram compactados sem perda. Cada arquivo foi
verificado por SHA-256 antes de remover sua cópia descompactada. Manifestos:
`var/gerenciador/backups-compactados-ingestao-20261024.json` e
`var/backups/backups-compactados-ingestao-20261024.json`. A restauração pode usar
`gzip -dk caminho/arquivo.db.gz` (ou `.sqlite.gz`) quando houver espaço. Nenhum
histórico PostgreSQL, notícia, evidência ou tarefa foi excluído por essa compactação.

### Verificação da correção

Na medição de 24/10 às 13h46 (America/Sao_Paulo), o corpus passou de 7 para
**651 notícias com texto e evidência capturada**, conferidas diretamente no
PostgreSQL. Foram evitadas **7024 leituras de artigos**. Havia **172 padrões
calibrados em 37 fontes**; as demais fontes continuam sendo lidas sem esse corte.
As 160 rodadas em andamento foram preservadas e nenhum novo agendamento foi ligado.
Cinco análises receberam resposta inválida do provedor nas tentativas limitadas;
continuam registradas como falhas para revisão, sem descarte de texto/artefato.

Também foi corrigida a recuperação após indisponibilidade do PostgreSQL:
heartbeat não pode encerrar threads ou mascarar uma falha de checkpoint;
reconexões preservam mensagens não confirmadas. O processo monitora suas threads
para que uma saída inesperada provoque reinício pelo supervisor, em vez de deixar
um serviço aparentemente ativo com workers mortos.

Validação: testes de PostgreSQL/RabbitMQ, comparação em lote, rollback,
continuações XML, publicação independente, filtros temporais, parâmetros do
catálogo e parsers RSS/Atom; ruff/mypy; tela em navegador desktop e celular.
Relatórios locais: `var/ingestion/antes-otimizacao-20261024.json` e
`var/ingestion/depois-otimizacao-20261024.json`.
