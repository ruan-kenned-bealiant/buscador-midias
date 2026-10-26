# Conclusão dos candidatos Feeder pendentes — 26/10/2026

> Atualização de prontidão: os RSS comprovadamente funcionais passaram a participar da busca diária independentemente da idade da amostra; a data é filtrada em cada notícia. Veja [a verificação dos mecanismos](VERIFICACAO_MECANISMOS_20261026.md). As classificações mencionadas abaixo preservam o histórico da rodada.

Foram testados todos os **281 candidatos** que ficaram fora do orçamento da rodada inicial, em 16 fontes. O conjunto foi congelado a partir dos checkpoints anteriores; não houve nova descoberta de endereços. Os testes concluídos da rodada anterior foram preservados.

- **154 RSS aprovados e cadastrados**, todos com XML acessível, notícia textual do veículo e evidência validada.
- **127 candidatos sem comprovação nesta amostra**, sem cadastro ou ativação.
- Nenhum candidato ficou pendente por limite de quantidade nesta conclusão.
- O período não restringiu a idade da notícia: a recência apenas determina a classificação inicial da rota diária.
- Até três perfis HTTP por candidato e 20 segundos por teste, até três matérias por amostra. As fontes do mesmo host foram serializadas; oito grupos puderam trabalhar em paralelo.
- Todas as 154 comprovações passaram por releitura e conferência SHA-256. As cópias são identificadas por URL e conteúdo, evitando que capturas posteriores sobrescrevam outra versão da evidência.
- Dos 154 RSS cadastrados, 125 receberam classificação de rota diária e 29 ficaram em revisão por não haver publicação recente comprovada na amostra; todos os 154 permanecem ativos e tecnicamente comprovados.
- Os testes terminaram em aproximadamente 8 minutos e 39 segundos; a aplicação final foi concluída em seguida. As buscas agendadas continuaram desligadas, sem nova ingestão, chamadas ao Bright Data/OpenRouter ou escrita de notícias no corpus.

## Conferência

<http://127.0.0.1:8765/testes/feeder-pendentes>

Os 154 RSS também aparecem no gerenciador normal, vinculados às fontes e com mecanismo RSS e transporte HTTP comprovado. A evidência foi capturada como prova de funcionamento; isso não representa cobertura integral de cada veículo ou garantia permanente de acesso. No Washington Post, o RSS comprovado é da coluna de Carolyn Hax, e não da cobertura geral do jornal.

## Cadastros por fonte

| Fonte | RSS cadastrados |
|---|---:|
| Le Monde | 38 |
| The Guardian | 33 |
| The New York Times | 32 |
| BBC News | 19 |
| Deutsche Welle | 17 |
| The Wall Street Journal | 6 |
| Financial Times | 3 |
| El País | 3 |
| The Washington Post | 1 |
| la Repubblica | 1 |
| France 24 | 1 |

## Histórico e artefatos

A rodada inicial permanece em `var/gerenciador/mapeamento-feeder-20261026/`. Esta conclusão foi preservada separadamente em `var/gerenciador/feeder-pendentes-20261026/`, contendo `manifest.json.gz`, resultados por fonte, evidências HTML, `summary.json`, `application.json`, `candidatos.csv` e `report.html`. Os vínculos e testes aprovados estão no PostgreSQL, com eventos de auditoria e revisões anteriores preservadas.

O script `scripts/resume_feeder_candidates.py` usa checkpoints e um bloqueio para impedir retomadas simultâneas. Com `--apply`, só aplica comprovações válidas nas revisões congeladas. Reexecutá-lo após a conclusão gera o relatório e reutiliza os resultados existentes, sem repetir as buscas. Os 127 resultados negativos encerram esta rodada; não são automaticamente enviados para novas tentativas infinitas.
