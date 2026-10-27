# Otimização de descoberta e verificação no corpus

Plano baseado na leitura do fluxo e em consultas somente leitura em 26/10/2026.
Implementação autorizada posteriormente, preservando a rodada e suas seis filas.

## Implementação

- Descoberta e verificação não aguardam os backlogs de dedup/download. O PostgreSQL
  conserva as tarefas individuais; a publicação para RabbitMQ continua limitada.
  Download aguarda contexto e evidência aguarda gravação, ambos com backlog de 200.
- Verificação agrupa até 500 tarefas. Uma instrução SQL finaliza artigos, tentativas,
  tarefas e outbox, com validação de dono, geração, tentativa e despacho. Reentrega
  de um lote concluído não cria novas tarefas. Mensagens continuam contendo um ID.
- O publicador confirma o lote no RabbitMQ antes de atualizar outbox/tarefas em
  conjunto. Assim não mantém locks das primeiras tarefas enquanto espera os confirms
  das demais. Se o worker termina antes desse checkpoint, seu resultado é preservado;
  uma falha de confirmação mantém a possibilidade de reentrega idempotente.
- A consulta de completude usa `EXISTS` para entidades; só artigos incompletos
  carregam a análise e as entidades necessárias para reparar evidência.
- O serviço usa `psycopg_pool`, entre 2 e 12 conexões para transações curtas.
  Confirms RabbitMQ usam outro pool independente, entre 0 e 12 conexões; não disputam
  o pool dos checkpoints. Leases/locks durante HTTP usam conexões dedicadas fora deles.
- XML HTTP idêntico pode ser reutilizado por 30 segundos no mesmo processo, rodada,
  fonte, URL, transporte e validadores condicionais. Limites: 128 entradas, 32 MiB
  comprimidos e 4 MiB por resposta antes de comprimir. Falhas e 304 não são memorizados.
  Parsing, parâmetros, revisão, escopo e ocorrências continuam individuais.
- Não foram excluídos links sobrepostos nem aumentados os workers. Essas decisões
  dependem da auditoria de cobertura e de medição de recursos/hosts disponíveis.

Benchmark inicial em esquema descartável: finalizar 500 URLs novas levou 0,970 s
no caminho anterior e 0,117 s no caminho relacional novo. A comparação inclui commit,
sem chamadas a provedores. Esse ganho de checkpoint não representa ganho proporcional
do tempo total da rodada. Os logs `Lote dedup` distinguem consulta e checkpoint reais.

Regressões cobrem lote misto, reentrega, reserva substituída, rollback integral,
reutilização/rollback do pool, separação de leases e isolamento/expiração do cache.
Medições operacionais ficam em `var/maintenance/ingestion-fast-batch-20261026/`.

## Verificação na rodada em execução

O serviço foi atualizado cooperativamente, mantendo a rodada
`3727178f41234734aa62b1bd48243609` e o auditor de cobertura que aguarda seu encerramento.
Nos primeiros 30 segundos amostrados, dedup concluiu 2.192 tarefas e descoberta 163.
São vazões locais de aproximadamente 72 e 5,4 tarefas/s, não uma previsão da rodada.
Lotes reais de 500 URLs registraram 0,270 e 0,332 s entre consulta e commit do
checkpoint, sem incluir reserva, transporte RabbitMQ e espera em fila.

A descoberta voltou a usar 8–9 workers apesar de mais de 5 mil verificações pendentes.
O backlog de downloads pode crescer: ele representa trabalho descoberto mais cedo,
sem multiplicar a capacidade de acesso/contexto. Dois avisos posteriores de espera
por lock motivaram também a confirmação em conjunto no publicador. A validação não
permite afirmar que toda disputa de locks ou falha HTTP foi eliminada.
Esperas do broker também evidenciaram saturação temporária do pool compartilhado.
Por isso publicação e checkpoints passaram a ter pools independentes; o teste esgota
as 12 conexões curtas e confirma que o publicador continua obtendo sua conexão.

## Evidências observadas

- A rodada possui 20.186 links e 20.134 URLs raiz distintas. Reutilizar raízes idênticas
  é correto, mas essas 52 repetições exatas não explicam sozinhas o tempo da rodada.
- O corpus possui índice único em `url_hash`; entidades possuem índice em `article_id`.
- Uma consulta simples de 100 hashes levou aproximadamente 14 ms, com 59 resultados.
  É uma amostra, não uma medição da etapa completa de deduplicação.
- Abrir conexão, executar `SELECT 1` e encerrar levou 15–34 ms em cinco amostras.
  O fluxo abre várias conexões por tarefa/lote; pooling pode reduzir esse custo.
- `dedup_many` já consulta até 100 URLs de uma vez. A proposta não é apresentar esse
  agrupamento como novidade, e sim melhorar o transporte e a persistência do lote.
- `finish_many` usa transação e pipeline SQL, mas percorre `finish` por tarefa,
  incluindo consultas de reserva e atualizações individuais. Ainda há espaço para
  operações relacionais realmente em lote, a medir.
- A verificação no corpus pode pausar por backlog de 2 mil acessos; a descoberta pode
  pausar por backlog de 5 mil verificações. Essas etapas não estão independentes.
- HTTP condicional, cache de parsing, regras temporais e tarefas por XML já existem.
  A evolução deve medir seus resultados e corrigir lacunas, sem reimplementar tudo.

## Ordem recomendada

### 1. Desacoplar descoberta/verificação das etapas caras

Permitir que URLs e tarefas leves acumulem no PostgreSQL, sem fazer a verificação
aguardar o download de artigos ou a análise. Manter publicação gradual no RabbitMQ,
proteção de disco e contenção das etapas que produzem HTML/textos volumosos.

Definir um orçamento operacional para metadados no banco, com telemetria, em vez de
usar somente uma contagem fixa pequena como se uma URL e um HTML tivessem o mesmo
custo. O objetivo é terminar a enumeração e a conferência cedo; isso não aumenta
sozinho a capacidade final de análise/evidência.

### 2. Completar o processamento em lote da verificação

Manter a etapa e sua fila independentes, com resultado individual por URL. Testar
lotes de 100, 500 e 1.000, medindo duração completa até commit. Agregar a transferência
de IDs pelo broker ou reduzir round trips de leitura sem carregar HTML nas mensagens.

Trocar o caminho genérico de finalização por uma operação dedicada que atualize em
lote artigos, tarefas e tentativas e crie próximas tarefas/outbox na mesma transação.
Preservar dono, geração, despacho, idempotência, reparo de evidência e hashes legados.
Uma falha/reentrega de lote não pode duplicar gravações nem perder resultados parciais.

Separar a consulta de existência/completude da carga de entidades, resumos e pareceres:
para o caminho conhecido/completo, um teste de existência de entidades é suficiente;
o conteúdo completo é necessário para os artigos que exigem reparo/reutilização.
Aumentar consumidores de dedup somente depois de medir o lote e remover sua pausa
por backlog de acesso; de outra forma eles também ficariam bloqueados.

### 3. Reutilizar conexões PostgreSQL

Usar um pool limitado para transações curtas de reserva, consulta e checkpoint.
Não compartilhar simultaneamente a mesma conexão entre workers. Separar operações
que seguram transação/lock durante rede para não esgotarem o pool. Dimensionar pelo
limite real de conexões do banco e pelos demais serviços que o utilizam.

### 4. Ler menos documentos, com prova de contribuição

Usar a auditoria de cobertura para identificar RSS/sitemaps que acrescentam URLs e
rotas comprovadamente redundantes. Priorizar as rotas de atualizações e manter a
associação de todas as fontes de origem. Não excluir um caminho apenas por ter
amostra antiga ou sobreposição parcial com outro feed.

O cache atual é isolado por revisão/fonte. Avaliar uma camada compartilhada de HTTP
para URL e transporte equivalentes dentro da rodada, preservando separadamente
parsing, escopo editorial, parâmetros, revisões e ocorrências de cada link.

Nos índices, consolidar seletores temporais específicos quando o formato for
comprovado, como no g1. Não inferir publicação de notícia a partir de `lastmod`.
Ausência de data/formato conhecido deve continuar visível como limitação de cobertura.

### 5. Ajustar concorrência de rede com medição

Depois de reduzir trabalho e custo de banco, medir utilização dos nove workers de
XML e disponibilidade de hosts distintos. Elevar concorrência apenas se houver
hosts independentes prontos e recursos livres. Preservar serialização/intervalo por
host, Retry-After e orçamento de retentativas; não aumentar carga sobre um único site
para fazer o contador de descoberta cair.

## Critérios de validação

Comparar antes/depois com o mesmo conjunto de documentos capturados e hashes, sem
chamar OpenRouter/Bright Data. Persistência de benchmark ocorre em esquema descartável.
Depois, validar numa amostra HTTP finita, registrando cache frio/quente e estado de rede.

Medir URLs verificadas por segundo, XMLs por minuto, tempo em pausa, hosts ocupados,
tempo de HTTP/parsing, consulta ao corpus, reserva/checkpoint/commit e quantidade de
comandos/conexões por lote. Usar relógio monotônico para duração real, incluindo commit;
`now()` transacional não mede sozinho todo o tempo de execução da transação.

Aceitação: mesmos candidatos elegíveis e diagnósticos, mesmas origens, nenhum artigo
perdido/duplicado, retomada correta e maior vazão nessas etapas. Reportar separadamente
quanto tempo descoberta/verificação levam e quanto tempo o processamento completo leva.
Não prometer redução proporcional do tempo total quando o gargalo estiver depois delas.

## Validação de código

A regressão principal das cinco suítes de ingestão foi executada no PostgreSQL de
testes, com uma integração externa ignorada. Após ampliar o caso de reparo, 17 casos
focados passaram. O ajuste final de publicação/pools passou em outros 15 casos de
saturação, confirmação, recuperação e admissão por host. Ruff, formatação, mypy dos
oito módulos alterados e `git diff --check` passaram. Os testes usam portas falsas
para HTTP/contexto/evidência e não iniciam buscas pagas ou novas rodadas reais.

Após carregar os pools independentes, a amostra de 21h18 (America/Sao_Paulo)
registrou 53 conexões de um máximo de 100, sem novos erros nos 30 segundos
observados. A verificação alcançou 98.623 tarefas concluídas e nenhuma pendente
na fotografia final. Downloads, contexto, evidência e gravação continuaram avançando;
a rodada permanece aberta. A ausência de erro nessa janela não garante ausência
de novas falhas ao longo da execução. Registro: `final-pools.json` no diretório
de medições mencionado acima.
