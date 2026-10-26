# Descoberta complementar de RSS via Feeder — 26/10/2026

A rodada consultou as páginas públicas do diretório Feeder para os 65 veículos prioritários. O diretório foi tratado como indicação de candidatos; o teste de XML, notícia textual e evidência foi feito por HTTP diretamente nos veículos, com os mesmos leitores e capturador usados pelo gerenciador.

## Resultado

- 65 fontes consultadas; 52 páginas Feeder acessíveis. Ausência no diretório não significa ausência de RSS no veículo.
- 708 candidatos identificados.
- 126 já funcionais no catálogo e preservados.
- 168 candidatos testados: 82 comprovados e 86 sem comprovação nesta amostra.
- 81 RSS novos cadastrados e 1 recuperado, com mecanismo RSS e transporte HTTP efetivamente comprovado.
- 82 vínculos ativos em 21 fontes, conferidos novamente no PostgreSQL.
- WSJ e la Repubblica reativados após a comprovação dos seus links.
- 78 rotas de atualização; 4 RSS sem publicação recente comprovada ficaram com classificação de rota `review`. RSS antigo pode ser tecnicamente funcional, mas não comprova notícia do dia.
- 109 candidatos de terceiros/agregadores para revisão, 19 já vinculados a outra fonte e 5 fora do escopo editorial.
- 281 candidatos ficaram registrados sem teste por atingirem os limites desta rodada. Não foram ativados.

Foram preservadas 82 comprovações de evidência, correspondentes a 80 URLs únicas. Todos os hashes SHA-256 foram conferidos. O teste não inclui análise de contexto ou inserção das notícias no corpus. Não houve chamadas ao Bright Data/OpenRouter; as buscas agendadas continuam desligadas e nenhuma rodada de ingestão foi criada.

## Conferência no painel

<http://127.0.0.1:8765/testes/feeder>

O relatório apresenta, por fonte, os candidatos, RSS testados, comprovações, aplicação no catálogo e links para notícias e evidências. Os vínculos aprovados também aparecem no gerenciador normal.

## Vínculos aplicados por fonte

| Fonte | RSS adicionados ou recuperados |
|---|---:|
| The Wall Street Journal | 8 |
| The Guardian | 8 |
| Deutsche Welle | 8 |
| El País | 8 |
| Le Monde | 6 |
| g1 | 5 |
| The New York Times | 5 |
| la Repubblica | 5 |
| Euronews | 5 |
| Correio Braziliense | 4 |
| Metrópoles | 3 |
| Politico | 3 |
| BBC News | 3 |
| Financial Times | 3 |
| France 24 | 2 |
| O Globo | 1 |
| Agência Brasil | 1 |
| El Mundo | 1 |
| South China Morning Post | 1 |
| Al Jazeera English | 1 |
| Al Jazeera Arabic | 1 |

## Limites e critérios

Uma página pública por domínio, até 8 candidatos por fonte, até 120 segundos por fonte e 35 segundos por teste, até três perfis HTTP por candidato e três matérias por amostra. Respostas 429 encerram as tentativas daquele candidato; os leitores respeitam o intervalo e a pausa do servidor. Os feeds foram testados independentemente da idade da notícia; a recência só determina a elegibilidade da nova rota diária.

Origens registradas: domínio do veículo, distribuidor externo conhecido, agregador ou terceiro. Google Notícias, RSS-Bridge e similares não foram tratados como RSS oficial. Feeds montados pelo próprio Feeder sem URL RSS exportável foram excluídos da extração. Decisões manuais de exclusão/inativação, alterações de revisão e vínculos duplicados foram preservados. A amostra aprovada não garante cobertura integral ou disponibilidade permanente da fonte.

## Histórico e reprodução

Artefatos em `var/gerenciador/mapeamento-feeder-20261026/`:

- `manifest.json.gz`: fontes, revisões de links e mecanismos congelados; compactação sem perdas, sem credenciais.
- `feeder-pages/*.html.gz` e `discovery/*.json`: páginas públicas e procedência das sugestões.
- `sources/*.json`: todos os candidatos, classificações, tentativas, amostras e comprovações.
- `evidence/midias-ia/evidencias/*.html`: HTML das evidências com hash e base dos recursos.
- `applied/*.json`, `application.json`, `summary.json`, `candidatos.csv` e `report.html`: aplicação e consolidação.
- Os testes aprovados e revisões antes/depois também foram gravados em `link_test_results` e `events` no PostgreSQL.

`scripts/map_feeder_candidates.py` é retomável. Sem `--apply`, descobre/testa e gera os artefatos; com `--apply`, aplica os aprovados com conferência de revisão. Checkpoints concluídos não são testados novamente. Os limites mantêm o lote finito; retomar o mesmo lote não tenta automaticamente os candidatos que ficaram fora do orçamento.
