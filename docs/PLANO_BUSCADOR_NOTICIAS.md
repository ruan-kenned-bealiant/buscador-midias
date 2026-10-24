# Plano do buscador contínuo de notícias

Proposta de 24/10/2026. Este documento não inicia buscas nem altera o corpus.

## Referências conferidas

- KyX: `app/Services/MidiasIa/MidiasIaSearchConsumerService.php`,
  `MidiasIaTreatmentConsumerService.php`, `MidiasIaEvidenceConsumerService.php`,
  `MidiasIaArticlePersistenceConsumerService.php`, `MidiasIaTreatmentService.php`,
  `MidiasIaUrlCanonicalizer.php`, `MidiasIaItemCompleteness.php` e
  `docs/WIKI_FONTE_MIDIASIA.md`. Busca/tratamento/evidência/persistência usam filas
  RabbitMQ; tratamento reaproveita artigo/contexto conhecidos antes da captura.
- PowerDD: migrations e modelos `midias_ia_articles`, entidades, palavras-chave,
  fontes, contextos e evidências. Corpus deduplicado por `url_hash`. Contextos de
  diligência pertencem a artigo + tema + entidade, não à coleta geral.
- Python: `catalog/mechanisms/`, `catalog/testing.py`, `discovery/document_queue.py`,
  `domain/url.py`, `analysis/context.py`, `enrich/pipeline.py`, `evidence/capture.py`,
  `corpus/index.py`, `corpus/writer.py` e `criteria.py`. Já existem componentes
  reutilizáveis; o enriquecimento atual ainda agrupa etapas em lotes.

## Decisões propostas

Implementar em Python, com catálogo atual no PostgreSQL de Mídias, RabbitMQ para
as filas e tabelas operacionais persistentes no mesmo PostgreSQL. Usar um namespace
próprio de ingestão e filas diferentes das filas RC de diligências do KyX. Não criar
processos ou pareceres de clientes fictícios para aproveitar os consumers existentes.
O DDL continuará versionado e transportável para migrations do PowerDD.

Manter a análise geral já existente no Python: entidades, temas, resumo, fatos e
papéis. Pareceres específicos de entidade/cliente continuam no fluxo de diligências.
Preservar inicialmente o critério atual de entrada: texto de notícia comprovado e
pelo menos uma entidade identificável, havendo ou não fato adverso. Artigos sem
esse critério recebem motivo explícito de descarte, em vez de desaparecer do funil.

## Serviço e agendamento

O agendador é um processo supervisionado independente do servidor HTTP. Ele lê
sites/links ativos, mecanismo, revisão e periodicidade. Reserva links vencidos com
exclusão mútua e cria trabalho durável. Janela proposta inicial: últimas 48 horas,
configurável; permitir também rodadas manuais de datas fixas para recuperação.
As datas reais de cada janela são congeladas quando o trabalho nasce.

Não sobrepor leituras do mesmo link. A próxima consulta do link não precisa esperar
OpenRouter, evidência ou persistência de notícias da consulta anterior. Não produzir
uma tarefa para cada vencimento perdido durante uma interrupção: consolidar a
retomada sem perder o histórico nem criar tempestade de tarefas.

O serviço é contínuo, mas cada rodada é um lote fechado de links vencidos em um
instante. Novos vencimentos geram novas rodadas. Cada rodada termina quando seus
trabalhos chegam a estados finais, com distinção entre sucesso e pendências/falhas.
Uma fila de falhas não deve manter uma rodada com aparência de execução eterna.

## Filas independentes

1. `midias.ingest.discovery`: ler RSS/Atom/sitemap e emitir URLs candidatas. Um XML
   filho vira outra tarefa desta fila, liberando o worker. Deduplicar documentos
   dentro da consulta, seguir a profundidade configurada (até 10), respeitar escopo
   e robots. Profundidade/quantidade/tempo esgotados precisam gerar diagnóstico de
   cobertura parcial; trabalhos de continuação não podem perder filhos silenciosamente.
   Não usar o teste de três artigos como executor da coleta completa.
2. `midias.ingest.dedup`: canonicalizar como KyX, consultar hashes atual e legado,
   verificar corpus e reserva global das URLs em trânsito. Artigo existente e
   completo termina como conhecido. Para incompletos, programar apenas reparos
   identificados como necessários, sem refazer automaticamente todo o enriquecimento.
3. `midias.ingest.fetch`: baixar a página e extrair título, data, texto, idioma e
   metadados. Confirmar conteúdo textual; distinguir áudio/vídeo, bloqueio, erro e
   ausência de texto. Revalidar a janela pela data de publicação, sem confundir
   `lastmod` com publicação. Data desconhecida recebe estado/política explícitos.
   HTTP direto primeiro; fallback Bright Data configurável e auditado.
4. `midias.ingest.context`: analisar o texto via OpenRouter, com modelo, prompt,
   versão, tokens, custo e resultado por notícia. Resposta inválida ou falha do
   provedor deixa pendência/retry; não vira análise negativa. Análise somente de
   manchete não satisfaz o critério de entrada no corpus.
5. `midias.ingest.evidence`: reaproveitar HTML confiável da extração e evidências
   existentes; capturar novamente quando necessário conforme a política. Preservar
   o comportamento inicial de custo do Python: fallback pago de evidência para
   adversos, configurável e identificado separadamente do fallback de acesso.
   Salvar no armazenamento acessível pelo PowerDD, com hash e origem da captura.
6. `midias.ingest.persist`: gravar artigo e relacionamentos no corpus de Mídias em
   transação, com unicidade por `url_hash` e tratamento de concorrência com KyX.
   Preservar pareceres manuais, contextos e dados de outras origens. Registrar
   `article_id` no histórico da ingestão e associar todas as fontes/links de origem.

Data desconhecida: por padrão, tentar recuperá-la na página; se continuar ausente,
separar como pendência de data, sem afirmar que a notícia pertence à janela. A tela
pode oferecer inclusão explícita desses casos com identificação de data desconhecida.
Não descartar XMLs apenas porque seu nome/lastmod parece antigo sem evidência segura.

Falha de evidência não apaga texto nem análise. Depois das tentativas limitadas,
seguir a compatibilidade atual do corpus: evidência pode ficar failed/pending,
com reparo rastreável, e a notícia não é apresentada como completamente comprovada.
A política de evidência obrigatória versus pendente deve estar explícita e visível.

## Persistência operacional e entrega entre filas

Tabelas propostas: configurações e revisões; agenda por link; rodadas e consultas
por link; documentos XML; URLs candidatas; vínculo candidato/link/rodada; tarefas
por etapa; tentativas; análises; artefatos; consumo de provedores; outbox.

Mensagens RabbitMQ levam IDs e correlação, não HTML ou lotes enormes. Texto e HTML
ficam no armazenamento de trabalho, com retenção configurável. Cada etapa grava o
resultado e o próximo evento na mesma transação; um publicador entrega a outbox
com confirmação do broker. Reentregas são esperadas e tratadas idempotentemente.
Chaves de execução consideram etapa, URL, geração/versão e política de reprocessamento.

Workers têm heartbeat, timeout e retomada; falhas temporárias usam espera crescente
com tentativas limitadas. Ao esgotar, registrar na fila de falhas da etapa. Permitir
reprocessar só a etapa necessária, com histórico imutável. Credenciais/quota do
provedor indisponíveis devem pausar/desacelerar a fila afetada, sem reprovar todos
os sites. Concorrência e limites por host/provedor são independentes por etapa.

Nenhuma notícia nova na janela é um resultado bem-sucedido de coleta. Uma URL
repetida conserva todos os vínculos de origem, mas não cria análises/capturas pagas
simultâneas. A persistência reconfere o corpus para resolver corridas com o KyX.

## Tela Buscador de notícias

- Ativar/pausar serviço, janela móvel ou datas fixas para execução manual, filtros
  por fonte/link, disparo manual e revisão das configurações em minutos.
- Seis cartões de filas: aguardando, processando, concluídos, retries e falhas;
  idade do item mais antigo, heartbeat, taxa e última atividade.
- Rodadas fechadas com janela, links previstos/concluídos, descobertas únicas,
  repetidas/existentes, sem texto, fora da janela, sem critério, análises, evidências
  capturadas/pendentes e inserções no corpus. Identificar sucesso parcial.
- Detalhe por fonte, link e notícia: URL, etapa, tentativa, duração, diagnóstico,
  caminho da evidência, artigo no corpus, proveniência e próxima execução do link.
- Custos/tokens OpenRouter e chamadas Bright Data por etapa e rodada. Contagem de
  chamadas Bright Data não deve ser apresentada como custo faturado conhecido.
- Reprocessar falhas específicas; pausas sem apagar trabalho; indicação explícita
  de serviço parado e distinção entre pausar novos agendamentos e pausar workers.

Conciliação das contagens: descoberta pode repetir URLs por vários links; mostrar
ocorrências e notícias únicas separadamente. Cada notícia única deve possuir um
estado atual ou terminal e um motivo; não somar etapas como se fossem notícias
novas distintas. Explicar estes conceitos no Guia e dicionário.

## Ordem de entrega

1. Validar contratos de canonicalização, campos atuais do corpus, configurações
   e armazenamento de evidências; documentar divergências do legado. Criar o DDL
   operacional, outbox e contratos das mensagens.
2. Implementar serviço supervisor/agendador e filas de descoberta, deduplicação
   e extração. Conferir retomada e que todas as URLs elegíveis são consideradas.
3. Separar análise, evidência e persistência em workers próprios, reaproveitando
   os componentes existentes e mantendo o corpus compatível com KyX/PowerDD.
4. Entregar a tela, rastreamento por notícia/rodada, métricas e reprocessamento.
5. Validar ponta a ponta em esquema isolado: duplicações entre links, duas
   instâncias concorrentes, datas desconhecidas, fila/provedor indisponível,
   falha após commit antes de ack, erro de evidência e retomada após reinício.
6. Fazer um piloto real com fontes representativas e janela curta, conferir
   texto/análise/evidência no PowerDD e contagens/custos, depois habilitar todos
   os links ativos nas periodicidades cadastradas. Validar capacidade de banco,
   fila e armazenamento antes da carga integral.

Concluído quando as seis filas forem observáveis, o serviço retomar sem perder
trabalho, duplicações não repetirem gravações nem processamento pago desnecessário,
e cada ocorrência estiver associada a um destino ou motivo verificável.

## Atualização operacional — 24/10/2026

O fluxo atual usa o dia civil em Brasília, rotas de atualização e filas novas separadas do backlog anterior. A validação ponta a ponta passou e a primeira rodada acompanhada foi iniciada sem novos agendamentos automáticos. Consulte [FLUXO_DIARIO.md](FLUXO_DIARIO.md) para as regras, evidências e a preservação do histórico.
