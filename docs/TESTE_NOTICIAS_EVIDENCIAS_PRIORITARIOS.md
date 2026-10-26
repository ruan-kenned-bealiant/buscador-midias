# Teste de notícias e evidências — veículos prioritários

Teste HTTP finito em 26/10/2026, das 16h18 às 16h20 (Brasília), sem alterar catálogo, mecanismos, corpus ou agendamento. Foram usados os mecanismos e transportes congelados do catálogo. Para fontes sem rota editorial disponível, até três endereços oficiais/convencionais foram sondados como candidatos, sem cadastrá-los automaticamente.

- 65 fontes verificadas e 104 endpoints testados.
- 55 fontes com XML válido acessível.
- 40 fontes com notícia textual e evidência HTML validada.
- 38 fontes com evidência de notícia publicada no dia 26/10/2026.
- 79 evidências: 73 com publicação no dia, 2 anteriores e 4 sem data confirmada.
- 25 fontes sem evidência validada nesta amostra; não equivale a provar ausência de notícias ou impossibilidade permanente de acesso.

Foram examinadas até três matérias por endpoint e salvas até duas evidências por fonte. O capturador é o mesmo utilizado na ingestão: reaproveita o HTML recebido, valida identidade/texto, injeta a base dos recursos e grava a evidência com SHA-256. Todas as 79 evidências foram relidas e seus hashes conferidos. Não houve chamadas ao Bright Data/OpenRouter nem análise de contexto ou nova rodada de ingestão. HTML salvo não garante que todos os recursos externos do layout permaneçam disponíveis.

## Conferência visual

Abra <http://127.0.0.1:8765/testes/prioritarios>. Cada fonte apresenta os endpoints testados, diagnóstico e, quando houve sucesso, links da notícia e da evidência salva. As evidências usam o visualizador existente, com execução de scripts da notícia desabilitada.

## Limitações importantes

- NYT: evidências da edição chinesa, não comprovam a edição inglesa.
- Reuters: conteúdo Reuters Plus/publicitário não foi considerado prova editorial. O XML Graphics abriu, mas não confirmou notícia elegível nesta amostra.
- Agência Brasil e NHK: evidência textual confirmada, data de publicação não confirmada; fora da contagem de fontes com notícias de hoje.
- Cadastro “Record” aponta para record.com.br, de uma editora de livros; precisa de revisão de identidade antes de representar a emissora.
- GloboNews e outros cadastros por seção podem descartar URLs fora do prefixo da fonte. Não foram atribuídas notícias de outra seção para aumentar a contagem.
- Ausência de notícias na janela/escopo é separada de erro de acesso ao XML. Bloqueios de matérias, páginas com pouco texto, redirecionamentos e orçamento limitado são registrados.

## Resultado por fonte

| Fonte | Links testados | XML válido | Textos na amostra | Evidências | Evidências de hoje |
|---|---:|---|---:|---:|---:|
| Folha de S.Paulo | 1 | Sim | 3 | 2 | 2 |
| O Globo | 1 | Sim | 3 | 2 | 2 |
| O Estado de S. Paulo (Estadão) | 1 | Sim | 2 | 2 | 2 |
| Valor Econômico | 1 | Sim | 2 | 2 | 2 |
| Veja | 1 | Sim | 3 | 2 | 2 |
| g1 | 1 | Sim | 3 | 2 | 2 |
| UOL Notícias | 1 | Sim | 3 | 2 | 2 |
| R7 | 2 | Sim | 2 | 2 | 2 |
| Terra | 1 | Sim | 3 | 2 | 2 |
| GloboNews | 3 | Sim | 0 | 0 | 0 |
| CNN Brasil | 1 | Sim | 3 | 2 | 2 |
| Band Jornalismo | 3 | Não confirmado | 0 | 0 | 0 |
| BandNews TV | 1 | Sim | 2 | 2 | 2 |
| SBT News | 1 | Sim | 3 | 2 | 2 |
| Record | 3 | Não confirmado | 0 | 0 | 0 |
| Record News | 3 | Sim | 0 | 0 | 0 |
| Jovem Pan News | 1 | Sim | 3 | 2 | 2 |
| CBN | 3 | Não confirmado | 0 | 0 | 0 |
| Agência Brasil | 1 | Sim | 3 | 2 | 0 |
| Exame | 1 | Sim | 3 | 2 | 2 |
| Nexo Jornal | 3 | Sim | 0 | 0 | 0 |
| Poder360 | 1 | Sim | 3 | 2 | 2 |
| Metrópoles | 1 | Sim | 3 | 2 | 2 |
| The Intercept Brasil | 1 | Sim | 0 | 0 | 0 |
| Agência Pública | 1 | Sim | 3 | 2 | 1 |
| TecMundo | 3 | Sim | 0 | 0 | 0 |
| ge (Globo Esporte) | 1 | Sim | 2 | 2 | 1 |
| BBC News Brasil | 1 | Sim | 3 | 2 | 2 |
| A Crítica | 1 | Sim | 3 | 2 | 2 |
| Correio Braziliense | 1 | Sim | 3 | 2 | 2 |
| Folha de Pernambuco | 3 | Sim | 0 | 0 | 0 |
| Extra | 3 | Sim | 0 | 0 | 0 |
| GZH (Zero Hora) | 3 | Não confirmado | 0 | 0 | 0 |
| The New York Times | 1 | Sim | 3 | 2 | 2 |
| The Washington Post | 2 | Não confirmado | 0 | 0 | 0 |
| The Wall Street Journal | 3 | Sim | 0 | 0 | 0 |
| Associated Press | 1 | Sim | 3 | 2 | 2 |
| Reuters | 1 | Sim | 0 | 0 | 0 |
| Bloomberg News | 3 | Sim | 1 | 1 | 1 |
| USA Today | 2 | Não confirmado | 0 | 0 | 0 |
| Politico | 1 | Sim | 0 | 0 | 0 |
| BBC News | 1 | Sim | 3 | 2 | 2 |
| The Guardian | 1 | Sim | 3 | 2 | 2 |
| Financial Times | 3 | Sim | 0 | 0 | 0 |
| The Times | 2 | Não confirmado | 0 | 0 | 0 |
| Le Monde | 1 | Sim | 2 | 2 | 2 |
| Le Figaro | 1 | Sim | 3 | 2 | 2 |
| Der Spiegel | 1 | Sim | 3 | 2 | 2 |
| Deutsche Welle | 1 | Sim | 3 | 2 | 2 |
| El País | 1 | Sim | 2 | 2 | 2 |
| El Mundo | 1 | Sim | 2 | 2 | 2 |
| Corriere della Sera | 3 | Sim | 0 | 0 | 0 |
| la Repubblica | 1 | Sim | 2 | 2 | 2 |
| The Japan Times | 2 | Sim | 0 | 0 | 0 |
| Xinhua (English) | 2 | Não confirmado | 0 | 0 | 0 |
| China Daily | 2 | Não confirmado | 0 | 0 | 0 |
| South China Morning Post | 1 | Sim | 3 | 2 | 2 |
| ABS-CBN News | 2 | Sim | 0 | 0 | 0 |
| Al Jazeera English | 1 | Sim | 2 | 2 | 2 |
| Al Jazeera Arabic | 1 | Sim | 2 | 2 | 2 |
| EFE | 2 | Não confirmado | 0 | 0 | 0 |
| France 24 | 2 | Sim | 3 | 2 | 2 |
| Euronews | 1 | Sim | 3 | 2 | 2 |
| NHK World-Japan | 2 | Sim | 3 | 2 | 0 |
| CNN International | 1 | Sim | 0 | 0 | 0 |

## Artefatos e reprodução

- `var/gerenciador/teste-noticias-evidencias-20261026/manifest.json`: revisões, configurações e limites congelados, sem credenciais.
- `sources/<source_id>.json`: respostas e resultados por etapa, incluindo falhas e datas.
- `summary.json`, `fontes.csv` e `report.html`: consolidação e relatório navegável.
- `evidence/midias-ia/evidencias/*.html`: evidências isoladas do armazenamento do corpus; cerca de 33 MiB.
- `initial-no-candidates/`: checkpoints preservados antes da complementação dos quatro cadastros sem rota editorial.

Com os mesmos checkpoints, `PYTHONPATH=src:scripts .venv/bin/python scripts/probe_priority_news_evidence.py` regenera relatórios e retoma apenas fontes ainda não concluídas; não inicia uma nova ingestão. Para uma nova auditoria, usar outro diretório de saída preservando o histórico existente.
