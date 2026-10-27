# Redução de intervalos ociosos das filas — 26/10/2026

A medição em 12 amostras, separadas por aproximadamente dois segundos, confirmou
oscilações entre pausa e processamento. O contexto chegou a zero tarefas em execução
com trabalho pendente. A evidência manteve de dois a quatro workers ocupados e era
uma limitação importante naquele intervalo. Não foi detectada redução da configuração
de workers: descoberta 9, dedup 1, acesso 18, contexto 18, evidência 4, gravação 3,
além dos workers dedicados de recuperação.

## Causas e ajustes

1. Cada worker guardava sua decisão de capacidade por cinco segundos. Mesmo quando
   a etapa seguinte liberava espaço, esse worker podia continuar esperando. Agora
   há cache compartilhado **por etapa e processo**, válido por 0,5 segundo, com lock
   que evita repetir a mesma consulta simultaneamente para todos os workers. Erros
   de consulta não são guardados no cache.
2. Uma consulta vazia ao Rabbit aguardava até 0,5 segundo em eventos e mais 0,5 segundo
   antes de tentar novamente. A checagem de eventos agora é não bloqueante e a espera
   é de 0,25 segundo. Uma fila realmente vazia ainda faz espera, sem busy loop.
3. A evidência abria leitor e transação PostgreSQL, embora o capturador reaproveitasse
   apenas HTML local e tivesse Bright Data desabilitado. Essa abertura foi removida.
   Avaliação de texto, título, entidades, integridade e armazenamento continuam; as
   métricas registram zero novas requisições nessa etapa.

Os limites de backlog, controle por host, Retry-After, tamanho das filas e número
configurado de workers foram preservados. A descoberta ainda pode pausar legitimamente
quando as etapas seguintes estiverem cheias. O ajuste reduz espera desnecessária;
não elimina gargalos de rede, análise, avaliação de evidência ou disco e não permite
prometer uma duração total da rodada a partir de uma amostra curta.

## Validação e aplicação

- 13 testes de workers, incluindo concorrência do cache e evidência sem rede/banco.
- 15 testes existentes de captura de evidência.
- Quatro testes PostgreSQL em banco/esquemas descartáveis: fluxo completo por etapas,
  backpressure, recuperação e lease expirado.
- Ruff e mypy nos módulos alterados.

O serviço de ingestão é reiniciado cooperativamente para carregar o Python alterado.
A rodada `3727178f41234734aa62b1bd48243609` conserva seus checkpoints e mensagens.
Não há purge, criação deliberada de nova rodada ou mudança do universo de links.
A auditoria de cobertura permanece independente, aguardando o encerramento dessa rodada.

Amostras operacionais: `var/maintenance/ingestion-gaps-20261026/before.json` e
`after.json`, quando concluída a medição posterior. Contagens de tarefas em instantes
sucessivos podem variar; throughput de curto prazo não é benchmark controlado.

## Observação posterior

A mesma rodada retomou em estado `running`. Na segunda amostragem, o acesso às
notícias manteve 15–18 tarefas simultâneas em execução. Contexto e evidência
continuaram oscilando conforme o escoamento; descoberta permaneceu contida pelo
backlog da deduplicação. Foram concluídas 72 evidências nas duas amostras, cerca
de três por segundo. Portanto, não há ganho relevante de throughput final comprovado
nessa observação curta. Os ajustes retiram esperas e conexões dispensáveis, mas a
capacidade de evidência ainda limita o escoamento. A etapa de persistência estava
frequentemente sem novos itens, não acumulando fila grande.

## Ajuste posterior autorizado: deixar evidências acumularem

Foi removido o limite de backlog de evidências que pausava o contexto ao chegar
a 200 tarefas. A consulta de capacidade do contexto não abre mais conexão apenas
para contar esse backlog. Evidências pendentes permanecem no PostgreSQL e são
publicadas gradualmente (até 32 mensagens prontas no broker). Limites das outras
etapas, proteção de disco e quantidade de workers são preservados.
