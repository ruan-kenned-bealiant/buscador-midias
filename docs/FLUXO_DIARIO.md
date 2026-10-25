# Fluxo diário — ativação em 24/10/2026

A busca padrão considera publicações desde **00h no fuso America/Sao_Paulo até o
início da rodada**. Cada rodada conserva sua janela. O padrão não significa as
últimas 24/48 horas. O fuso pode ser editado no painel; notícias sem data ficam
fora do fluxo diário. Períodos manuais anteriores ao dia atual são rejeitados
nesse modo. O modo interno legado `lookback` permanece disponível aos testes de
arquivo, mas não é exposto como rotina histórica no produto.

## Descoberta e seleção

- RSS, news sitemap e g1 diário são rotas elegíveis de atualização. Feeds em modo
  automático também são elegíveis. Outros modelos precisam de uma seleção
  explícita em `ingest_daily_routes` para o agendamento geral.
- A seleção explícita tem os propósitos `updates`, `archive` e `review`. Ela não
  exclui nem inativa os cadastros do gerenciador.
- O mecanismo do g1 seleciona suas partições diárias conhecidas e a sobreposição.
- Outros índices, no modo diário, selecionam os filhos cujo `lastmod` indica
  modificação desde o início da janela. Filhos sem indicação temporal são
  sinalizados como cobertura parcial, sem expandir todo o arquivo.
- `lastmod` escolhe candidatos; **não comprova a publicação**. A data explícita de
  publicação do RSS/news sitemap e a extração do HTML determinam a elegibilidade.
  URLs sem publicação no XML precisam ser verificadas no HTML. A estratégia não
  garante cobertura completa quando o portal omite ou informa mal seus metadados.
- Datas explícitas do XML fora da janela são descartadas antes de criar tarefas.
  A data inferida do nome de uma URL não é, sozinha, comprovação de publicação.
- O XML completo interpretado fica em cache com revisões da configuração. Se o
  servidor oferecer ETag/Last-Modified, é feita revalidação condicional. Conteúdo
  idêntico ou resposta 304 dispensa nova interpretação.

As seis filas continuam separadas: descoberta, deduplicação, texto, contexto,
evidência e persistência. A deduplicação consulta o corpus em lotes. A publicação
nas filas de descoberta/texto admite somente uma tarefa em trânsito por host,
permitindo trabalhar entre sites sem provocar disputas repetidas pelo mesmo host.

## Prova completa

O teste isolado processou cinco notícias do dia do O Globo pelas seis etapas.
**As cinco foram gravadas no corpus com texto, entidades e evidência capturada.**
A repetição da mesma janela não criou novas tarefas de deduplicação, análise ou
captura. O relatório registra datas, URLs, IDs do corpus, evidências, tempos,
consumo de análise e resultados por etapa:

`var/ingestion/daily/smoke/bm_sources_pilot_20261024_195647_02485e/resultado.json`.

## Preservação do trabalho anterior

As tabelas operacionais antigas foram movidas em uma única transação para:

`midias_ingest_archive_20261024_195902`.

Foram preservados **161 rodadas, 447.265 tarefas, 350.137 registros operacionais
de artigos e 358.146 ocorrências**, além das tentativas, outbox e demais tabelas.
As tabelas do corpus e do catálogo não foram movidas nem apagadas. O manifesto
contém as contagens de cada tabela:

`var/ingestion/daily/archive-manifest.json`.

As filas antigas `midias.ingest.*` permanecem no Rabbit sem novos consumidores.
O fluxo diário usa `midias.daily.v1.*` e tabelas operacionais novas no schema
`midias_manager`. O índice de arquivos fica em `ingest_archives`.

No painel, **Ver rodadas anteriores preservadas** consulta o histórico pelo
endpoint `/api/gerenciador/busca/historico`. O arquivo mantém o estado original
registrado nas rodadas; esse estado não significa que seus workers estão ativos.
O detalhamento completo permanece nas tabelas arquivadas, somente para consulta.

O trabalho local do fluxo diário usa `var/ingestion/daily/work`, separado do
caminho antigo. As configurações de namespace e diretório são carregadas do
arquivo privado `var/ingestion/rabbit.env`; ele não deve ser publicado.

## Primeira rodada acompanhada

Rodada: `04e70b0cd3d548e6b6aa5a01930ae7bb`.

Janela: **24/10/2026, 00h–17h00 de Brasília**.

Nove links nas fontes Folha, O Globo, Estadão, CNN Brasil, Agência Brasil,
Poder360, Metrópoles, The Guardian e Al Jazeera English. Essa seleção inicial não
representa a ativação de todos os sites do catálogo. A NPR ficou fora dessa
rodada devido à falha de acesso às páginas constatada no piloto.

Os workers estão habilitados para consumir essa rodada. **Novos agendamentos
automáticos continuam desabilitados**, evitando abrir outras rodadas antes da
avaliação desta. A periodicidade cadastrada nos links foi preservada.

Acompanhe em `http://127.0.0.1:8765/buscador`. Datas fora da janela, ausência de
notícia nova e conteúdo sem texto têm resultados próprios; não são tratados como
falha de acesso. Erros reais permanecem identificados na etapa correspondente.

Durante o acompanhamento inicial houve interrupções de conexão ao PostgreSQL.
As tarefas com leases remanescentes foram recuperadas após parar o processo;
a mesma rodada foi retomada. A distribuição por host foi ajustada para reduzir
as disputas observadas no início. Nenhuma fila antiga foi purgada.

## Calendário de quatro rodadas

O modo `clock` está configurado para **1h, 6h, 12h e 16h**, no fuso de Brasília.
Cada rodada considera publicações desde 00h até sua execução. A deduplicação
reaproveita as notícias concluídas nas rodadas anteriores. A coluna de intervalo
individual de cada link é preservada para uso futuro; nesse modo não determina
quando iniciar uma rodada.

Horários e modo são editáveis na configuração do buscador. Cada horário executado
fica registrado em `ingest_clock_runs`, impedindo uma segunda execução automática
do mesmo horário, inclusive após reinício. A checagem ocorre a cada dez segundos,
com tolerância de quinze minutos. Horários perdidos fora dessa tolerância não são
repostos: a próxima rodada cobre o dia desde 00h.

O calendário está salvo, com novos agendamentos ainda pausados (`enabled=false`).
A execução manual continua disponível sem consumir os horários automáticos.

## Duas visões das rodadas

No buscador há duas abas: **Por etapa** e **Por site**. A seleção de rodada é
compartilhada. Na visão por site, a ausência de seleção usa a rodada mais recente.
Há busca pelo nome do site, paginação e atualização automática a cada cinco
segundos enquanto a página está visível.

Cada site mostra links lidos, URLs únicas, resultados e uma barra de progresso.
Durante a descoberta, a barra é indeterminada porque o total ainda pode crescer.
Depois, contabiliza uma unidade por link descoberto e cinco por URL única:
verificação no corpus, texto, contexto, evidência e gravação. Resultados terminais
como notícia já conhecida, fora da janela ou falha resolvem as unidades restantes,
sem afirmar que as etapas dispensadas foram executadas. A barra é progresso de
processamento, não percentual de cobertura do site nem estimativa de tempo.

Uma fonte chega a 100% quando não tem trabalho aberto na rodada. **Concluída com
pendências** aparece em destaque quando houve falhas; 100% não significa ausência
de falhas. Reabrir uma tarefa pode reduzir o progresso. A mesma URL encontrada
em vários links do mesmo site é contada uma vez; tarefas compartilhadas entre
fontes são visíveis nas origens correspondentes.

**Detalhar etapas** exibe aguardando, processando, retentativa, concluídas, falhas
e ignoradas. **Ver tarefas** abre a lista filtrada por rodada, site e etapa,
permitindo consultar o diagnóstico e as tentativas. A visão geral também passa
a respeitar a rodada e o site selecionados nos seus contadores.


As repetições após falhas usam seis filas adicionais com sufixo `.recovery`,
uma por etapa, com um worker de recuperação por etapa. As filas principais
recebem tarefas sem falha anterior; contenção de host não consome tentativas
nem transfere uma tarefa para recuperação. Falhas de provedor adiadas também
seguem para recuperação, preservando o orçamento de tentativas. O limite de
tentativas, a pausa por indisponibilidade e a exclusão mútua por domínio seguem
valendo nas duas filas. As etapas seguintes de uma tarefa recuperada voltam à
fila principal para sua primeira tentativa.

O painel mostra separadamente tarefas principais e de recuperação pendentes.
A rodada só termina quando ambas escoarem; “Recuperação em andamento” indica
que apenas as repetições estão pendentes naquele instante. Elas ainda podem
descobrir notícias e gerar novas tarefas principais. Nenhuma nova rodada é
criada para recuperar falhas.

A configuração operacional de análise passou de três para seis workers
principais, mais um de recuperação. A chave OpenRouter foi consultada antes da
mudança (ativa, sem teto configurado); limites do provedor continuam sujeitos
a respostas 429 e à pausa automática do serviço. Essa concorrência é editável
no gerenciador. Aumentar a concorrência aumenta a velocidade de consumo dos
créditos, sem alterar o modelo ou criar análises duplicadas intencionalmente.


O fallback Bright Data está desativado na busca de notícias, inclusive para
reprocessamentos que referenciem configurações antigas. O acesso aos documentos
e às notícias usa apenas HTTP direto; a evidência reaproveita o HTML salvo.
Cada tarefa permite no máximo três tentativas totais (inicial + duas repetições),
sem uma camada adicional de retries HTTP. O painel permite reduzir esse limite.
Contenção de host, sem execução da chamada, continua sendo adiada sem gastar
o orçamento. Os contadores históricos de Bright Data permanecem preservados.
