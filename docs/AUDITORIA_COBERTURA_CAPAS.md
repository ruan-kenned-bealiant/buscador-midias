# Cobertura observada — 120 veículos prioritários

A auditoria acompanha a rodada `3727178f41234734aa62b1bd48243609`. A referência é
congelada **enquanto a rodada ainda processa**, e a comparação definitiva espera
seu encerramento (`completed` ou `partial`, com `finished_at`). Não inicia ingestão.
A espera consulta a rodada a cada **3 minutos** e avança automaticamente quando
detecta seu encerramento. `status.json` informa o instante da próxima verificação.

Painel: **http://127.0.0.1:8765/testes/cobertura**. Enquanto a captura acontece,
o relatório informa a quantidade de fontes observadas e não é um resultado final.
Atualize a página para acompanhar. O serviço independente é
`buscador-midias-coverage-frozen-3727178f.service`.

## Referência e limites

[coverage_priority_sites.csv](../data/coverage_priority_sites.csv) contém 120 IDs
únicos: 60 fontes brasileiras e 60 internacionais. É um recorte de relevância e
diversidade editorial, não um ranking estatístico de audiência. Inclui inativas.

Para cada fonte, o auditor abre a capa e até duas editorias **presentes no menu**,
priorizando política, economia, Brasil/mundo e tecnologia. Intercala candidatas das
páginas, para que a capa não ocupe sozinha a amostra. Não inventa caminhos estaduais
nem faz exploração recursiva de seções. URLs publicitárias conhecidas (Reuters Plus,
CreativeGroup, patrocinadas) não contam como notícia editorial. O cadastro Record
que aponta para editora de livros é sinalizado como revisão de identidade.

São até 60 candidatas, 25 artigos comparáveis, 120 segundos e 96 requisições por
fonte; oito fontes em paralelo. O leitor limita respostas a 4 MiB. Provas comprimidas
têm orçamento de 1 GiB e reserva de 512 MiB de disco. Há até dois perfis para a capa:
o transporte cadastrado e HTTP direto, compartilhando o orçamento. Limites atingidos
ficam explícitos: não significam leitura integral nem falha do veículo.

HTML bruto comprimido, data de observação, hash e resultado HTTP são preservados.
`references/{id}.json` não é reescrito na comparação ou na conferência. A retomada
reutiliza esses arquivos. Não usa RSS/sitemap como referência de cobertura: eles são
os mecanismos avaliados, e não o denominador da avaliação.

**Limite temporal:** a janela desta rodada termina em 26/10/2026 às 19h05 de Brasília.
Notícias publicadas depois não são lacunas dela. Uma notícia anterior ao corte,
mas encontrada na capa horas depois, também não prova que já estava disponível no
feed no início da rodada. Portanto, a métrica é observacional e as lacunas de
fornecimento são hipóteses a investigar, não prova de cobertura total ou defeito.

Capas bloqueadas ou dependentes de JavaScript, datas desconhecidas e texto não
confirmado ficam inconclusivos, fora do denominador. Esta etapa não renderiza um
navegador. Texto/data reutilizam os extratores da aplicação; não constituem uma
validação independente da precisão desses extratores.

## Comparação por notícia

Quando a rodada termina, o auditor consulta o corpus e as ocorrências/tarefas da
**mesma rodada**, usando as referências congeladas. O corte de criação no corpus é
`finished_at`, não o instante posterior em que o relatório foi consultado.

A identidade considera hashes atuais e legados, URL final e canonical dentro do
escopo editorial. Artigos já conhecidos contam como cobertos, sem simular nova
captura. A tabela mostra a última situação conhecida de cada etapa por URL, além
do estágio final. Estados de tarefas são observados no momento da consulta; não há
reconstrução histórica de cada atualização da tarefa.

| Diagnóstico | Interpretação |
|---|---|
| `corpus_new` | Artigo registrado durante a rodada, até o corte |
| `corpus_already_known` | Artigo já existente antes da rodada |
| `discovery_gap` | Referência comparável ausente da descoberta da rodada e do corpus |
| `processing_failure` | Descoberta ocorreu, mas uma tarefa terminou com falha/cancelamento |
| `policy_exclusion` | Descarte explícito por conteúdo ou ausência de entidade |
| `extraction_or_date_gap` | Aplicação encerrou por texto/data/janela, mas a referência foi confirmada |
| `processing_pending` | Há trabalho ainda aberto para a URL |
| `processing_unresolved` | Descoberta sem resolução identificada |
| `after_window` / `before_window` | Publicação fora da janela, não é falta da rodada |
| `date_unknown` / `text_not_confirmed` | Não é possível compor referência comparável |
| `access_inconclusive` / `budget_inconclusive` | Referência limitada por acesso/orçamento |

O relatório apresenta **artigos no corpus / referências comparáveis**, por exemplo
18/25. Também separa descoberta e descartes de política. Sem referência comparável,
a porcentagem é nula/inconclusiva, nunca zero ou 100%. O agregado é ponderado pelo
número de notícias; sempre deve ser lido com a quantidade de fontes comparáveis,
bloqueadas e limitadas. Não equivale à cobertura de todas as notícias dos 120 sites.

## Uma correção e uma conferência, com fim definido

Depois da comparação, somente fontes com `discovery_gap` recebem uma passagem de
rotas oficiais. Falhas de processamento, identidade e bloqueios ficam na lista de
lacunas para tratamento específico; não provocam busca interminável de caminhos.

A passagem examina até oito candidatos por fonte, obtidos de links RSS/sitemap nas
páginas congeladas e de dois caminhos convencionais (`sitemap.xml` e `feed/`). Não
consulta buscadores nem provedores pagos. Cada candidato lê no máximo o XML raiz
e sete filhos declarados; não expande recursivamente arquivos históricos. Há quatro
fontes em paralelo, orçamento de 180 segundos e 96 requisições de exploração por
fonte, mais até oito testes normais de no máximo 20 segundos/16 requisições cada,
sempre condicionados ao tempo restante da fonte. Um último teste pode ultrapassar
o orçamento por alguns segundos. Isso limita o custo, mas pode deixar alternativas
válidas fora da amostra.

Uma rota só é aprovada se:

1. O XML realmente enumera uma URL ausente da referência, por identidade de URL.
2. O teste normal do mecanismo confirma XML, notícia e texto.
3. Uma única conferência reencontra ao menos uma dessas URLs no XML.

Uma rota aprovada **nova** pode ser cadastrada, ativa, com teste arquivado, prova,
mecanismo, classificação de atualização diária e intervalo de 240 minutos.
Índices de sitemaps ficam como `index_collection_plan_review`: encontrar notícia
em alguns filhos não calibra os ramos temporais para buscas futuras. Não são
ativados automaticamente por esta amostra. A fonte precisa continuar ativa e na mesma
revisão. Links já existentes, inclusive excluídos/inativos, e fontes inativas são
preservados; esta auditoria não os restaura. Configuração do mecanismo alterada
invalida a gravação. Inserção, teste e evento são atômicos.

As provas dos artigos de referência já confirmados e do XML ficam no diretório da
auditoria. O teste normal confirma acessibilidade/texto; não usa análise de contexto.
Isso não é gravação de evidência ou notícia no corpus. Rotas recuperadas indicam
capacidade para **buscas futuras** e nunca aumentam retroativamente a cobertura da
rodada encerrada. Nenhuma nova rodada ou tarefa de ingestão é disparada.

Não há segunda passagem de correção automática. Resultados inconclusivos e lacunas
restantes são preservados. Checkpoints e lock impedem repetição infinita ou execução
simultânea no mesmo diretório. A fase inicial sem `--freeze-now` continua disponível
para auditorias que desejem esperar antes da captura; `--correct-gap-routes` exige
`--freeze-now`.

## Artefatos

Pasta: `var/gerenciador/cobertura-capas-3727178f/`.

| Arquivo | Conteúdo |
|---|---|
| `manifest.json` | Alvos, janela, limites e corte de comparação |
| `before-freeze-*.json` | Manifesto/estado do auditor anterior preservados |
| `catalog-before.json` | Revisões das fontes e configurações dos mecanismos |
| `status.json` | Capturando referência, aguardando rodada, corrigindo ou concluído |
| `references/{id}.json` | Referência imutável por fonte |
| `sources/{id}.json` | Resultado da comparação por fonte |
| `html/*.html.gz` | Provas de referência e XML, separadas das evidências do corpus |
| `sites.csv` | Matriz por fonte, numerador/denominador, limites e motivo |
| `articles.csv` | Comparação por notícia |
| `gaps.json` | Lacunas de descoberta, extração/data e processamento |
| `corrections/{id}.json` / `corrections.json` | Tentativas, teste, conferência e aplicação de novas rotas |
| `summary.json` / `report.html` | Métricas e relatório navegável por veículo |

```bash
systemctl --user status buscador-midias-coverage-frozen-3727178f.service --no-pager
journalctl --user -u buscador-midias-coverage-frozen-3727178f.service -n 30 --no-pager
```

Executores: [audit_frontpage_coverage.py](../scripts/audit_frontpage_coverage.py) e
[coverage_gap_routes.py](../scripts/coverage_gap_routes.py).
Regras puras: [analysis/coverage.py](../src/buscador_midias/analysis/coverage.py).
Validação: 26 testes da auditoria, incluindo transação real em esquema descartável do banco
exclusivo de testes; Ruff e mypy nos módulos da auditoria.
Os testes das rotas do painel também foram executados (41 testes no total).
