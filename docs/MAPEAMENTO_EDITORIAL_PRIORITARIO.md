# Mapeamento editorial prioritário — 26/10/2026

Atualização posterior: os perfis curl foram aplicados ao teste, à Saúde das fontes e à
interpretação das próximas buscas. Para os resultados após a recuperação, consulte
[COBERTURA_PRIORITARIOS_APOS_RECUPERACAO.md](COBERTURA_PRIORITARIOS_APOS_RECUPERACAO.md).
Os números e as ressalvas abaixo preservam a fotografia anterior com o cliente HTTP original.

Rodada encerrada. Foram auditados **65 veículos**, reunindo grandes jornais, portais, emissoras e agências do Brasil e do exterior. A seleção é editorial/prática, não um ranking de audiência nem uma lista exaustiva de veículos relevantes.

**32 fontes** entregaram ao menos uma notícia com data nas últimas 72 horas e texto confirmado nesta amostra; **32** tiveram prova textual de publicação de hoje (calendário de Brasília). **3** entregaram apenas texto antigo/sem data e **30** ficaram sem comprovação textual nesta rodada.

## Resultado aplicado

- 8 links novos de atualização foram cadastrados; 1 link excluído automaticamente foi restaurado.
- Foram verificadas/atualizadas 84 provas de links existentes (contagem de operações, não de URLs distintas).
- El País foi reativado; CNN International foi cadastrada e ativada com seu sitemap editorial.
- Aliases de redirecionamento foram deduplicados. Exclusões manuais, revisões posteriores e classificações prévias de arquivo foram preservadas.
- Folhas datadas e índices genéricos grandes ficaram para arquivo/revisão, sem promovê-los a rotas diárias apenas por uma amostra positiva.
- A correção no leitor de RSS resolve os destinos explícitos `redir.folha.com.br/.../*https://...` apenas nos feeds oficiais da Folha e apenas para destinos editoriais do domínio permitido. Não altera controles de acesso.

## O que ficou comprovado — e o que não ficou

“Texto recente comprovado” exige acessar o RSS/sitemap, obter uma URL de artigo, abrir o artigo e confirmar texto útil com data de publicação. HTTP 200, XML válido, título ou resumo isolados não bastam. Texto útil extraído não garante a íntegra do artigo, especialmente em páginas com assinatura.

Cada fonte recebeu orçamento finito: até 180 s, 64 chamadas físicas, 24 sondagens e 6 testes de mecanismo; 10 fontes em paralelo, com serialização por host. A conferência adicional foi uma única passagem por 12 fontes, até 100 s por fonte, com endereços dirigidos. Respostas do mesmo endereço foram reaproveitadas somente dentro da passagem e seus corpos originais foram preservados comprimidos, com SHA-256. No complemento, 502/503 tiveram no máximo uma repetição adicional; recusas permanentes não foram repetidas.

Foram **1369 chamadas HTTP físicas** e **259 testes de mecanismo**, sem Bright Data e sem OpenRouter. São **111 endereços distintos por destino final** com ao menos um texto recente na amostra; isso inclui caminhos já existentes, aliases normalizados e algumas folhas/índices que não foram promovidos ao fluxo diário. Não é uma contagem de links novos nem de cobertura integral.

Não foi iniciada uma nova rodada de ingestão. A classificação automática de contexto e a gravação no corpus não fazem parte desta auditoria de acesso. A última rodada de ingestão e o agendamento desativado foram preservados.

## Principais ressalvas

- **Reuters:** o noticiário editorial atual segue sem prova de coleta. Os índices principais retornam 401; o sitemap de Graphics entrega conteúdo histórico de 2024. Os antigos links `/plus/` são comerciais e não demonstram cobertura do noticiário.
- **g1:** os endereços testados, inclusive o índice central com `g1_daily`, retornaram 503. A falha ocorreu antes da interpretação do mecanismo. O caso exige nova verificação quando o acesso normalizar; não foi tratado como ausência de notícias.
- **NYT:** a prova recente veio da edição chinesa. A edição principal retornou 403 nos artigos amostrados.
- **Bloomberg:** RSS/XML funcionais, mas artigos amostrados com 403; não há comprovação textual atual.
- **CNN International:** um artigo textual recente confirmado; houve também notícia bloqueada e amostra sem texto suficiente. Funcionalidade parcial.
- **France 24:** texto recente no RSS francês; o inglês não confirmou texto útil. O cadastro do link francês foi preservado porque sua revisão não estava no congelamento inicial; a prova completa está nos artefatos.
- **Corriere:** amostra desta auditoria não confirmou notícias recentes, mas há artigos de hoje no corpus. Esse contraste mostra por que uma amostra negativa não deve desativar uma fonte inteira.

## Conferência do corpus existente

No congelamento inicial havia **1214 artigos textuais publicados hoje**, atribuídos a **26 destas fontes** pela primeira fonte registrada na ingestão. Isso é dado real do corpus, separado das URLs candidatas e das amostras HTTP. A contagem não inclui artigos atribuídos a outra fonte/fluxo e não significa que foram gravados durante esta auditoria. Para as duas fontes acrescentadas no complemento, a coluna inicial aparece como “—”.

## Resultado por fonte

As colunas de links e notícias abaixo descrevem somente a amostra desta rodada; “0” não prova que o site não publica ou que nenhum outro caminho funciona. “Corpus hoje” é a fotografia inicial, não um acréscimo causado pelo mapeamento.

| Fonte | Resultado | Links com texto recente | Notícias de hoje na amostra | Corpus hoje |
|---|---|---:|---:|---:|
| Folha de S.Paulo | Texto recente comprovado (amostra) | 5 | 9 | 4 |
| O Globo | Texto recente comprovado (amostra) | 3 | 5 | 236 |
| O Estado de S. Paulo (Estadão) | Texto recente comprovado (amostra) | 5 | 12 | 42 |
| Valor Econômico | Texto recente comprovado (amostra) | 5 | 6 | 55 |
| Veja | Texto recente comprovado (amostra) | 4 | 6 | 12 |
| g1 | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| UOL Notícias | Texto recente comprovado (amostra) | 1 | 3 | 6 |
| R7 | Texto recente comprovado (amostra) | 6 | 3 | 0 |
| Terra | Texto recente comprovado (amostra) | 6 | 9 | 8 |
| GloboNews | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| CNN Brasil | Texto recente comprovado (amostra) | 5 | 3 | 0 |
| Band Jornalismo | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| BandNews TV | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| SBT News | Texto recente comprovado (amostra) | 3 | 5 | 11 |
| Record | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Record News | Texto recente comprovado (amostra) | 4 | 1 | 0 |
| Jovem Pan News | Texto recente comprovado (amostra) | 5 | 7 | 16 |
| CBN | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Agência Brasil | Texto recente comprovado (amostra) | 5 | 2 | 4 |
| Exame | Texto recente comprovado (amostra) | 4 | 6 | 25 |
| Nexo Jornal | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Poder360 | Texto recente comprovado (amostra) | 2 | 3 | 9 |
| Metrópoles | Texto recente comprovado (amostra) | 3 | 5 | 90 |
| The Intercept Brasil | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Agência Pública | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| TecMundo | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| ge (Globo Esporte) | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| BBC News Brasil | Texto recente comprovado (amostra) | 1 | 1 | 4 |
| A Crítica | Texto recente comprovado (amostra) | 1 | 2 | 1 |
| Correio Braziliense | Texto recente comprovado (amostra) | 2 | 2 | 0 |
| Folha de Pernambuco | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Extra | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| GZH (Zero Hora) | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| The New York Times | Texto recente comprovado (amostra) | 1 | 3 | 1 |
| The Washington Post | Somente texto antigo/sem data na amostra | 0 | 0 | 0 |
| The Wall Street Journal | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Associated Press | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Reuters | Somente texto antigo/sem data na amostra | 0 | 0 | 0 |
| Bloomberg News | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| USA Today | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Politico | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| BBC News | Texto recente comprovado (amostra) | 4 | 4 | 10 |
| The Guardian | Texto recente comprovado (amostra) | 5 | 9 | 79 |
| Financial Times | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| The Times | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Le Monde | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Le Figaro | Texto recente comprovado (amostra) | 5 | 5 | 70 |
| Der Spiegel | Texto recente comprovado (amostra) | 5 | 5 | 43 |
| Deutsche Welle | Texto recente comprovado (amostra) | 6 | 10 | 12 |
| El País | Texto recente comprovado (amostra) | 1 | 1 | 0 |
| El Mundo | Texto recente comprovado (amostra) | 3 | 8 | 62 |
| Corriere della Sera | Somente texto antigo/sem data na amostra | 0 | 0 | 162 |
| la Repubblica | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| The Japan Times | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Xinhua (English) | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| China Daily | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| South China Morning Post | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| ABS-CBN News | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| Al Jazeera English | Texto recente comprovado (amostra) | 4 | 7 | 50 |
| Al Jazeera Arabic | Texto recente comprovado (amostra) | 4 | 9 | 1 |
| EFE | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| France 24 | Texto recente comprovado (amostra) | 1 | 3 | — |
| Euronews | Texto recente comprovado (amostra) | 1 | 3 | 201 |
| NHK World-Japan | Sem comprovação textual nesta rodada | 0 | 0 | 0 |
| CNN International | Texto recente comprovado (amostra) | 1 | 1 | — |

## Artefatos e repetição

- [Resumo estruturado e notas por fonte](../var/gerenciador/prioritarios-editoriais-20261026/resumo.json)
- [Planilha por fonte](../var/gerenciador/prioritarios-editoriais-20261026/fontes.csv)
- [Planilha por endpoint, mecanismo e exemplo](../var/gerenciador/prioritarios-editoriais-20261026/links.csv)
- [Operações aplicadas](../var/gerenciador/prioritarios-editoriais-20261026/aplicacao.json)
- `manifest.json`: revisões congeladas e fotografia inicial do corpus.
- `testes/` e `complemento/`: resultados completos, datas, texto confirmado, status e exemplos.
- `corpos/<fonte>/<sha256>.gz`: corpos HTTP originais, inclusive respostas de bloqueio.

`scripts/map_editorial_priority.py` faz a auditoria sem alterar catálogo/corpus; checkpoints evitam repetições. `scripts/apply_editorial_priority.py --apply` aplica provas positivas com auditoria e controle de revisão. Esta execução usa diretório/data próprios; uma futura rodada precisa de novo manifesto/diretório e revisão explícita dos alvos. Não é um coletor contínuo.

Validação de código: 119 testes passaram; lint dos arquivos alterados e mypy dos dois módulos de domínio/leitura passaram.
