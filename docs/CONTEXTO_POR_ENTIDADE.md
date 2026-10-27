# Contexto de cada entidade na notícia

O buscador identifica entidades no artigo sem receber uma pessoa/empresa alvo. A
classificação geral (`finding`, temas, resumo e categoria) não determina o parecer
de todos os envolvidos. Um investigado pode ter `alerta=true`, enquanto a vítima e
a autoridade citadas na mesma notícia têm `alerta=false` e justificativas próprias.

## Execução

1. A etapa `context` identifica entidades e classifica o artigo.
2. Para artigos elegíveis, avalia as entidades em lotes de até **4**, com chamadas
   OpenRouter separadas. A identificação admite até 30 entidades: 30 produzem uma
   chamada de identificação e oito chamadas de avaliação individual, nove no total.
3. O sistema extrai até quatro trechos literais de até 600 caracteres por entidade,
   contendo seu nome. Envia esses trechos com números e pede à IA apenas `trecho_id`.
   A resposta é validada por entidade: booleano explícito, confiança finita,
   justificativa e referência válida a trecho da própria entidade. O texto da prova
   vem do recorte local, não é transcrito, traduzido ou parafraseado pelo modelo.
   Vítima/autoridade/testemunha/citado não recebem alerta por associação. Citações
   inventadas, respostas truncadas e entidades ausentes deixam trabalho pendente.
   Para `alerta=false` explícito, fato adverso nulo é normalizado para vazio;
   alerta positivo continua exigindo fato adverso não vazio.
4. Salva progresso depois de cada lote, inclusive os itens válidos de um lote parcial.
   Nova tentativa reutiliza a identificação e pede apenas as entidades sem contexto.
   Alterar modelo, prompt ou entrada invalida esse reaproveitamento. Cada checkpoint
   valida dono, tentativa, geração e despacho e renova o lease.
5. Somente após completar os contextos identificados segue para evidência e gravação.
   As seis filas permanecem. O consumo de todas as chamadas entra nas métricas da
   tentativa; em falhas, o consumo permanece no histórico daquela tentativa.

`entity_context_progress.validation` e as métricas da tentativa guardam contadores de
validação, sem a resposta bruta: `invalid_fields`, `quote_not_literal`,
`name_not_in_quote`, `incompatible_alert`, `requested`, `accepted` e `missing`.
São calculados nas respostas das próprias tarefas, sem chamadas extras de diagnóstico.
O leitor ainda aceita respostas com `trecho_evidencia` literal, sujeitas à mesma
validação, mas o prompt atual pede a referência numerada para reduzir erros de cópia
e tamanho da saída. Referência inexistente, de tipo inválido ou sem trecho nominal
correspondente deixa a entidade pendente.

Cada avaliação usa limite de saída próprio: `300 + 650 × entidades_do_lote` tokens.
O lote máximo de quatro admite 2.900 tokens. Isso reduz risco de truncamento, sem
prometer que qualquer modelo sempre respeitará o contrato. Falha de JSON ou conteúdo
incompleto segue a política limitada de retentativas; não vira um parecer negativo.
Na última tentativa o job grava os pareceres que o validador aceitou e lista os demais em
`entity_context_missing`; a rodada fica `partial`. Entidade rejeitada (por exemplo,
`incompatible_alert`: alerta para papel não adverso) não ganha parecer, e se nenhuma
validou o job falha como antes. Medido em 27/10: ~13% das tentativas voltavam com lote
pendente e a maioria desses jobs terminava `failed`, perdendo também as entidades válidas.
O transporte também recusa `finish_reason=length/max_tokens`, mesmo que o fragmento
devolvido ainda seja JSON válido. O consumo cobrado dessa resposta continua registrado.
Truncamento não é tratado como indisponibilidade global do provedor: fica pendente
na própria tarefa, sem acionar o disjuntor da etapa.

## Gravação no corpus

Na mesma transação do artigo, entidades e palavras-chave, o writer grava um contexto
por identidade em `midias_ia_article_contexts`. Uma falha desfaz aquele artigo inteiro.
Não foi necessário criar tabela: é a relação existente do banco de Mídias.

| Campo | Significado neste fluxo |
|---|---|
| `article_id` | Artigo do corpus |
| `entity_name`, `entity_type`, `entity_hash` | Menção nominal; tipo `pf`/`pj` e hash compatível com o KyX sem documento |
| `entity_document` | `NULL`: não inferimos CPF/CNPJ de um nome |
| `match_nome` | Nome confirmado no texto; não confirma a identidade civil de um homônimo |
| `alerta`, `confianca`, `justificativa` | Avaliação individual, inclusive para entidades sem fato adverso |
| `papel_entidade`, `fato_adverso` | Participação dessa entidade e fato adverso atribuível a ela |
| `termos_relacionados` | Temas adversos individuais, não herdados do artigo |
| `raw_context` | Parecer, trecho literal, produtor, versão do prompt e escopo `article_entity` |
| `process_id`, `client_id`, `group_id` | `NULL`: não há consulta de cliente/processo |
| `theme_id`, `theme`, `tema_confirmado` | `NULL`: não foi solicitado um tema específico de busca |
| `ai_model`, `evaluated_at` | Modelo e data da avaliação |

A chave existente é `(article_id, COALESCE(theme_id,0), entity_hash)`. A reentrega
atualiza somente o contexto genérico que este buscador produziu. Não sobrescreve
contextos de outros escritores, de clientes/processos ou de temas específicos.
Pareceres e evidências anteriores também não são apagados por uma gravação legada
sem avaliações individuais. `WriteOutcome.contexts` conta registros inseridos/atualizados.

A identidade segue `MidiasIaInputBuilder::entityHash()` e `Document::sanitize()` para
nomes com caracteres ASCII: não translitera antes de sanitizar. Nomes cujo resultado
sanitizado é vazio usam fallback nominal com SHA-256 para evitar colisão entre todos
os nomes não latinos; esse fallback não equivale a uma identidade documental do KyX.

## Limites e convivência

- A entrada continua sendo o recorte configurado em `analysis_text_chars` (padrão
  atual: 12 mil caracteres na ingestão). Não se garante identificar toda entidade
  de uma matéria maior. Há limite de 30 entidades identificadas nesta implementação.
  Ao atingir esse limite, `entity_limit_reached=true` e a tarefa sinaliza
  `coverage_partial`: pode haver outras entidades que o recorte/limite não representou.
- As chamadas extras aumentam consumo e latência de contexto. O checkpoint evita
  pagar novamente por lotes já completos, mas não elimina o custo das novas avaliações.
- Contextos nominais sem tema não substituem a confirmação de identidade/documento
  e tema de uma futura diligência. O Treatment atual do KyX consulta o tema específico;
  este ajuste não altera o KyX/powerDD para reaproveitar contextos genéricos como se
  fossem confirmações de uma consulta direcionada.
- Artigos/avaliações já gravados não são reclassificados automaticamente. Análises
  antigas reaproveitadas no reparo de evidência permanecem antigas, sem inventar um
  parecer por entidade. Reprocessamento histórico exige execução explícita.
- O fluxo legado de enriquecimento/sincronização continua aceitando registros sem
  contextos individuais; o fluxo novo descrito aqui é a ingestão do gerenciador.

## Validação

Testes com provedor simulado cobrem investigado versus vítima, prova inventada,
recorte enviado, JSON truncado e limite de lote. Testes de ingestão em PostgreSQL
cobrem 30 entidades, retomada parcial, mudança de modelo e reserva substituída.
Testes do writer em esquema descartável conferem idempotência, campos nulos de
escopo, preservação de parecer alheio/temático e rollback integral. Não são prova
de qualidade do modelo em todas as notícias reais.

## Aplicação operacional em 26/10/2026

O serviço foi recarregado cooperativamente mantendo a rodada
`3727178f41234734aa62b1bd48243609`. A estrutura real e o índice de escopo foram
conferidos sem alteração de DDL. A primeira verificação encontrou 39 contextos de
16 artigos; havia também respostas individuais incompletas, preservadas para
retentativa. Não é uma garantia de que todas as notícias já possuam contextos.

Passaram 116 testes de análise/conversão/identidade, 21 do writer em esquema
descartável e os casos PostgreSQL da ingestão, incluindo retomada de 30 entidades
e a passagem pelas seis etapas. Ruff, mypy dos dez módulos e `git diff --check`
passaram.

Diagnósticos usam os checkpoints e contadores das respostas da rotina existente.
Artefatos locais: `var/maintenance/entity-contexts-20261026/`.

O formato com seleção de trechos usa a versão
`context-2026-10-26.3-entity-context-refs`. A versão efetiva acompanha a análise até
a gravação; uma tarefa antiga não ganha a versão nova só por ser persistida depois.

Na fotografia de 26/10 às 22h01 (America/Sao_Paulo), a versão com referências
tinha 441 contextos gravados em 67 artigos, dos quais 14 com alerta. Os checkpoints
da versão mostravam 200 itens aceitos e 10 pendentes nos últimos lotes registrados
por artigo. Isso mede validação de contrato, não acurácia factual do modelo nem
cobertura total das entidades das notícias. Consulte `reference-runtime.json`.
