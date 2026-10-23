# Design

## MVP do produto: gerenciador de sites

O produto terá busca de notícias e gerenciador de sites. A interface atual contém
o gerenciador em `/` e `/gerenciador`, com CRUD, estados ativo/inativo, filtros, paginação,
evidências e teste interativo. A configuração dos mecanismos fica em `/mecanismos`. As demais telas foram removidas; os arquivos históricos
e APIs de consulta são preservados. A busca completa será integrada depois.

O cadastro editável usa SQLite independente em `var/gerenciador/catalogo.sqlite`,
adequado ao MVP local sem depender do PostgreSQL do corpus. Importação transacional
única do retrato funcional e catálogo CSV; IDs estáveis, vínculo fonte-mecanismo,
unicidade de endereço e revisão para evitar sobrescritas concorrentes. Cadastros
novos não recebem comprovação automaticamente. Alterar URL/tipo invalida a prova
atual; a auditoria conserva os valores anteriores. Inativação da fonte é herdada
em consulta e mantém a escolha individual dos links. Exclusão lógica e eventos
preservam rastreabilidade, sem modificar a rodada 12 ou relatórios do mapeamento.

A API do gerenciador é a única área com escrita; as rotas históricas continuam
somente leitura. Escritas limitadas a HTTP local, JSON de tamanho limitado e token
de mesma origem. O MVP não oferece autenticação, restauração de excluídos pela
interface nem coleta contínua. Publicação e uso multiusuário exigirão
autenticação e revisão da infraestrutura. Não cria agendamentos ou custos externos.

### Mecanismos separados e configuração persistente

Cada modelo implementa o contrato `Mechanism` em módulo próprio: automático, RSS,
sitemap direto, índice e news sitemap. As regras de aceitação, expansão e ajuste do tipo
ficam nesses módulos. Parsing defensivo, executor, rede e persistência são compartilhados.
A interface cria os campos a partir do esquema de parâmetros do próprio mecanismo.
Modelos sem expansão não expõem parâmetros de árvore.

`mechanisms` mantém parâmetros globais e revisão, inicializados idempotentemente
sem alterar os endpoints existentes. Escritas validam esquema e revisão com auditoria.
Novos testes congelam os valores e a revisão; o resultado salvo conserva esse contexto.
Alterações de configuração não apagam provas ou mudam o status dos links. A consulta
marca testes de configurações anteriores. A aplicação de um teste ou rascunho confere
atomicamente a revisão global e a revisão/configuração do endpoint.

O tamanho configurado é aplicado ao transporte e à descompressão/parsing. Duração e
requisições limitam a rede; quantidade de XMLs, profundidade, ordem e quantidade de
amostras controlam o executor. Regras de robots, domínio/IP público e TLS são invariantes.

### Modelos e validação sob demanda

Cinco modelos versionados em código: automático, RSS/Atom/RDF, sitemap direto, árvore de
sitemaps e sitemap de notícias. Configuração por endpoint, independente do estado ativo.
Migração idempotente adiciona `model` e `last_test` ao cadastro existente, inferindo apenas
formato/índice. Modelos novos não recebem comprovação retroativa.

O botão Testar inicia um trabalho e consulta progresso por polling. Até dois trabalhos
simultâneos, sem fila ilimitada, com limite de documentos, requisições, bytes, tempo e
profundidade 10. Cada filho é deduplicado; referências de índice não são notícias.
Notícias precisam pertencer à fonte configurada. A prova de texto usa a classificação e
extração existentes, incluindo rejeição de áudio dedicado e páginas de bloqueio. A idade
da notícia é independente da legibilidade. Um resultado vazio em XML válido não é erro.
O resultado registra `assessment_version`, `usable_for_news`, `article_samples` e três
`checks` (acesso, notícia, texto), com estados confirmado, não confirmado ou não testado.
`usable_for_news` exige XML válido e compatível e pelo menos uma amostra de notícia
textual. Acessibilidade do artigo e classificação ficam registradas por amostra; áudio
é excluído da contagem de artigos úteis. XML sozinho, página HTML genérica, zero amostras
ou lista vazia não conferem a confirmação de busca textual. O selo verde é exclusivo
dessa comprovação com a revisão atual. Resultados antigos não são reescritos; a UI pede
um novo teste quando a avaliação completa não foi registrada.

Rede direta: robots antes do destino, DNS público fixado na conexão com Host/SNI originais,
redirecionamentos validados, sem cookies/credenciais/proxies de outros serviços. Persistência
separada de rede: nenhum lock de banco fica aberto durante downloads. Provas são aplicadas
somente à configuração e revisão que originaram o teste; rascunhos persistem ao salvar.
Resultados atuais e evidência histórica são apresentados separadamente.

## Modelo relacional destinado a Mídias

O cadastro utiliza tabelas físicas `sites`, `links` e `mechanisms` no SQLite local.
`links.source_id → sites.id` e `links.model → mechanisms.model` são FKs obrigatórias.
Um site tem vários links; cada link seleciona um mecanismo reutilizável.
`link_test_results` referencia link/mecanismo e preserva parâmetros, revisão e resultado
dos testes salvos; `links.last_test` é cache compatível com a API atual. Auditoria e
exclusões lógicas preservam origem, versões e evidências. Parâmetros e snapshots são
JSON em colunas; entidades e relações têm tabelas e constraints reais.

`catalog/schema.py` migra transacionalmente os nomes antigos, estabelece FKs e recupera
os testes já salvos na auditoria local. `schema_migrations` torna a operação idempotente.
A estrutura atual é provisória: o destino definitivo é o banco de Mídias, cuja conexão
não é utilizada aqui. Adaptador, DDL nativo, transporte, conversão de tipos e validação
completa antes da troca são trabalho posterior obrigatório. Ver
[modelo relacional e roteiro de migração](docs/MODELO_RELACIONAL_CATALOGO.md).

## Validação completa anterior à migração

A qualificação operacional dos links é uma rodada finita, com fila e tentativas
persistentes ligadas ao cadastro. Janela móvel de 365 dias por tentativa, três
mecanismos/tentativas no máximo, prova real de XML → notícia → texto e estimativa
conservadora de periodicidade. Aplicação ao final com revisão otimista, exclusão
lógica, inativação dos sites vazios e preservação integral das evidências anteriores.
As tabelas de rodada/tentativa e `link_cadence` também integram o modelo destinado
futuramente a Mídias. Não há coleta contínua ou agenda automática. Ver
[critérios, limites e retomada](docs/VALIDACAO_COMPLETA_CATALOGO.md).

## Referência histórica do mapeamento

As descrições seguintes registram decisões anteriores; interfaces citadas foram removidas
do produto atual, preservando dados, análises e APIs de compatibilidade.

## Objetivo

Consolidar RSS/sitemaps utilizáveis nas futuras buscas das 1.294 fontes, associados ao
mapa de editorias e subseções por evidência observada. Distinguir mecanismos recentes,
complementares, históricos e lacunas; páginas HTML não substituem essa entrega.
O inventário termina com seus arquivos reutilizáveis e limitações, sem coleta contínua
ou retomada automática.

## Seleção após a rodada 12

Na continuação da busca, usar fila durável por documento com alternância entre fontes,
uma leitura por host em paralelo e 16 workers. Um índice enfileira seus filhos e libera o
worker; não percorre um site inteiro dentro de uma tarefa. Confirmar resultados e filhos
em checkpoint SQLite, com deduplicação por mecanismo e URL, profundidade máxima de 10 e
janela fixa de 48 horas. Gravar as URLs recentes no estado durante a leitura e reutilizar
as respostas HTTP em cache persistente dentro desta mesma rodada. XMLs sem data permanecem
no cache, com contagens e amostras, sem alimentar a fila de notícias recentes como se fossem
novos. O painel mostra documentos verificados e mecanismos concluídos além das fontes.
O procedimento desta rodada fica em `var/ciclo-busca-20261019T005450Z/executar_fila.py`,
com checkpoint `fila-documentos.sqlite` e evidências por mecanismo; não cria agendamento.

Após o encerramento da rodada 12, analisar os resultados da janela de 48 horas para
selecionar os RSS/sitemaps que efetivamente conduzem a notícias textuais úteis. Conferir
amostras das páginas de notícia: XML válido ou links listados, isoladamente, não comprovam
utilidade. Registrar a evidência e o motivo de manter, excluir ou deixar pendente cada mecanismo.

Excluir mecanismos dedicados a áudio/podcasts, como o exemplo da Bloomberg relatado pelo usuário.
Em mecanismos mistos, separar as matérias textuais dos conteúdos de áudio; a presença de áudio
em um endpoint não exclui automaticamente os demais mecanismos ou a fonte inteira. Manter
complementos que acrescentem notícias úteis após a deduplicação.

Fonte acessível sem notícia nova na janela permanece como acesso bem-sucedido, sem erro;
essa ausência isolada não justifica exclusão permanente. Distinguir conteúdo inadequado,
falta de data, bloqueio e falha técnica. Preservar o inventário original e produzir um plano
selecionado separado. Esta seleção começa após o término da rodada, sem alterar a busca em curso.

## Consulta do mapa

O painel abre a tela de mapeamento em `http://127.0.0.1:8765`:

```bash
scripts/bm.sh dashboard
# Catálogo alternativo: scripts/bm.sh dashboard --sites caminho/sites.csv
```

A tela mostra os **1299 registros do catálogo**: 1294 fontes únicas e cinco duplicatas
identificadas. A visão inicial **Acesso às notícias** mostra o resultado da auditoria sem
filtro de data: **858 fontes** com página de notícia acessível e **830** com texto legível,
com **717** confirmadas por RSS e **699** por sitemap (há sobreposição entre esses grupos).
Ao selecionar uma fonte, aparecem os mecanismos, links de notícias testadas, resultado HTTP,
evidências de extração, limites e motivos de falha. Não testados aparecem explicitamente,
sem serem classificados como falhas. Há busca, filtros por país, situação da fonte, tipo
RSS/sitemap e resultado da auditoria, além de paginação e download da planilha completa.
A aba **Descoberta na web** apresenta as consultas, páginas oficiais e comparação de
editorias. As abas **RSS e sitemaps** e **Associação às seções** preservam o inventário funcional e
as associações observadas. Fontes sem mecanismos continuam visíveis.

As rotas `/` e `/mapa` servem esta tela; `/fluxo` preserva o painel operacional anterior.
`/api/mapa/fontes` consulta o catálogo e `/api/mapa/secoes?site=CHAVE` fornece as seções paginadas.
Essas consultas abrem o estado somente leitura e não disparam buscas nos sites.
`/api/mapa/acesso?site=CHAVE` prioriza os relatórios locais de `var/validacao-final` após o fechamento,
com as auditorias anteriores preservadas em suas pastas, com `kind=all|feed|sitemap`,
`mode=all|accessible|readable|unconfirmed|untested|partial`, busca `q` e paginação `offset`/`limit`.
`/api/mapa/auditoria.xlsx` baixa a planilha consolidada. A API de acesso não abre o banco
nem faz requisições aos veículos. Sem os arquivos locais, informa que a auditoria está
indisponível; os arquivos não são gerados ao abrir a tela.

## Validação final de ponta a ponta

Rodada fechada em `2026-10-19T00:34:21.202342+00:00`, sem continuação automática. O catálogo foi
congelado em **1.294 fontes únicas**. As **38.849 linhas** do
inventário correspondem a **38.501 mecanismos por fonte**, após deduplicação de
URLs canônicas e unificação de sitemap/news sitemap no mesmo endereço. Inclui mecanismos
anteriormente bloqueados; não limita a execução aos XMLs já classificados como funcionais.

| Evidência desta rodada, sem somar resultados antigos | Total |
|---|---:|
| Mecanismos com verificação direta ou XML observado na expansão | 7.132 de 38.501 |
| Mecanismos com XML interpretado | 5.814 |
| Mecanismos que levaram a notícia acessível | 4.588 |
| Mecanismos com texto extraído | 4.351 |
| Fontes com notícia acessível | **858** |
| Fontes com texto extraído | **830** |
| Fontes com XML interpretado | 1.035 |
| Fontes sem mecanismo no inventário | 138 |
| Mecanismos não iniciados no prazo | 31.369 |
| Percursos tentados, mas incompletos | 757 |

Das entradas verificadas, **7.055** tiveram
execução própria e **77** já tinham XML interpretado
como filho de índice nesta mesma rodada. Estas últimas foram creditadas sem novas
requisições e sem inferir acesso a notícias; permanecem com percurso individual incompleto.

RSS confirmou acesso em **717 fontes**, sitemap em
**699**; os grupos se sobrepõem. Foram testadas
**14.037 URLs de notícia normalizadas por fonte**, das quais
**11.683** acessíveis e **10.538** com texto.
O mesmo endereço pode atender mais de um mecanismo e não é contado como notícia distinta
na mesma fonte. **2.088 seções não gerais** tiveram notícia
acessível na comparação por caminho da URL, entre as seções previamente identificadas
por menu/metadados. Isso não representa cobertura integral de editorias.

A fila foi reorganizada durante a execução, sem alterar as entradas nem o prazo original,
para priorizar mecanismos com exemplos conhecidos e alternar escopos do mesmo domínio.
As evidências obtidas antes da reorganização foram preservadas.

Método: RSS/Atom/RDF e sitemaps interpretados; índices percorridos até nível **10**
(entrada no nível zero); até **três notícias por mecanismo em cada passagem**, distribuídas entre primeira,
intermediária e última candidata retida. Sem filtro por data de publicação ou palavra-chave.
Notícia acessível exige identificação e ausência de erro/desafio; texto exige os critérios
existentes de extração e confiabilidade. HTTP 200 sozinho não confirma notícia.

Limites: **45 minutos globais**, até 32 domínios simultâneos no fluxo principal, uma correção isolada e intervalo de um segundo por
host, 16 documentos XML/entrada, 64 chamadas HTTP/entrada, 90 segundos para iniciar
requisições por entrada, 8 MiB por resposta e até 5.000 candidatas retidas por mecanismo em cada passagem.
O limite de chamadas global foi 150.000. Apenas filhos declarados por índices são seguidos;
menus HTML e pesquisa web não ampliam este inventário. Robots, exclusões e escopo são
respeitados. Um mecanismo pode ter notícia comprovada e percurso incompleto simultaneamente.

O contador registra um teto conservador de **33.104 chamadas HTTP**, incluindo uma reserva de 64 chamadas para a entrada que gerou exceção antes da reorganização. A contagem exata dessa entrada não foi preservada. Foram **2701.0 segundos** de
execução de rede; a consolidação local ocorre depois. Foram repetidas
**572 entradas** com erro transitório, de
**574 elegíveis**, uma vez dentro do prazo global. Evidências das duas
passagens ficam preservadas; a comprovação mais forte dentro desta rodada é mantida.
Não há coleta contínua, análise por IA, gravação de notícias no corpus ou agendamento.

Foi corrigida uma exceção de data fora do intervalo na normalização UTC. XMLs e páginas
com esse metadado preservam URLs e texto, descartando somente a data inválida. Os testes
de regressão de RSS, sitemap e HTML passaram; a entrada da WAM foi verificada novamente
e confirmou notícia com texto, ainda no prazo original.

**Fechamento:** entradas não iniciadas e percursos limitados continuam inconclusivos e
não são promovidos a falhas nem a cobertura confirmada. Fontes sem RSS/sitemap conhecido
continuam no relatório; esta rodada não pesquisa mecanismos para elas. Resultado positivo
comprova um caminho observado, sem garantir completude, atualização futura ou acesso permanente.
Os totais de 838/804 da revisão web abaixo são históricos; esta rodada é um retrato separado.

A tela **Acesso às notícias** passa a mostrar esta rodada após a consolidação, com até
três amostras por mecanismo, HTTP, extração e filtro **Verificações incompletas**. A aba
**Validação por seção** mostra correspondências e exemplos atuais. Os filtros do catálogo
incluem **Validação final: incompletas** e **Sem RSS/sitemap no inventário**.
`/api/mapa/validacao-secoes?site=CHAVE` aceita `mode=all|confirmed|unconfirmed`, `q`,
`offset` e `limit`; lê somente arquivos locais. As abas de inventário e descoberta
preservam o contexto das rodadas anteriores.

Entrega: [`var/validacao-final/resultado.xlsx`](var/validacao-final/resultado.xlsx), com
abas Fontes, Mecanismos, Notícias testadas, Documentos XML, Seções e Critérios e resumo.
CSVs, notícias listadas comprimidas, plano congelado, hashes de entrada, resultados por
mecanismo e evidências da primeira passagem estão na mesma pasta. Os planos reutilizáveis
[`plano-xml-validado.json`](var/validacao-final/plano-xml-validado.json) e
[`plano-com-noticias.json`](var/validacao-final/plano-com-noticias.json) distinguem XML
interpretado do subconjunto que levou a notícias; podem ser fornecidos explicitamente a
`discover --discovery-plan CAMINHO`, com os limites e filtros próprios daquela busca.
O botão **Baixar plano com notícias** serve o segundo plano em
`/api/mapa/plano-validado.json?mode=news`; `mode=xml` fornece o plano de XMLs interpretados.
Nenhuma estratégia do banco foi substituída.

## Revisão web limitada de todas as fontes

Rodada concluída em **18/10/2026** para as **1.294 fontes únicas**, agrupadas em
**1.264 domínios**. Foram enviadas **2.528 consultas planejadas**, duas por domínio,
para localizar diretórios oficiais e mecanismos. Páginas que publicam listas de RSS
como artigos também foram consideradas. Além da rodada, houve seis consultas iniciais
de diagnóstico da ferramenta e duas solicitações de diagnóstico que falharam na conexão.

Resultado: **1.038 novas associações fonte–mecanismo em 217 fontes**, abrangendo **994
endereços distintos**, dos quais **989 eram inéditos no inventário global**. Os demais
cinco endereços já eram conhecidos em outro escopo do catálogo. Aliases foram resolvidos
antes de persistir os destinos; a comparação por fonte usa chave e escopo do catálogo.

| Histórico após a revisão web, antes da validação final | Fontes |
|---|---:|
| XML funcional no inventário reutilizável | **1.039** |
| Pelo menos uma notícia acessível comprovada | **838** |
| Também com texto legível | **804** |

A revisão ampliou mecanismos de fontes já acessíveis, além de confirmar XML em nove
fontes que antes não tinham mecanismo funcional. Ainda há **255 fontes únicas sem XML
funcional confirmado**. Isso não demonstra inexistência de mecanismos nem impossibilidade
de acesso. Não se mede cobertura integral por esses totais.

Foram lidas **1.157 páginas HTML oficiais distintas**, com leitura de alguma página HTML
em **981 fontes**. Houve **22.392 associações de editorias observadas nos menus**
(22.327 URLs distintas), das quais **2.272** tiveram correspondência amostral com URLs
listadas em mecanismos ou exemplos do inventário. Ausência de correspondência permanece
inconclusiva: mecanismos conhecidos podem ter apenas exemplos armazenados, menus podem
ser dinâmicos e as páginas acessíveis não representam necessariamente todas as editorias.

Seis domínios ficaram sem resultado retornado pelo serviço de busca por erro HTTP 500:
`newvision.co.ug`, `observer.ug`, `newtimes.co.rw`, `chronicles.rw`, `igihe.com` e
`m28investigates.com`. As 12 consultas correspondentes estão registradas como falhas de
pesquisa, sem classificá-las como ausência de RSS/sitemap. A validação interna dos
1.264 domínios terminou sem exceções; os bloqueios e limites observados permanecem nos
relatórios. Sete páginas HTTP 200 foram reclassificadas como desafio/erro, usando somente
o HTML salvo, e não contam como páginas oficiais lidas.

Limites: até três páginas oficiais, 16 XML, dois links de notícia e 32 chamadas por
domínio, até dez níveis de índices, 90 segundos para iniciar novas requisições por domínio,
sem corte de data e com robots.txt respeitado. Foram **6.650 chamadas HTTP na validação
direta**; não inclui as operações internas do serviço de busca. Feeds declarados para
hosts externos foram registrados para revisão, sem ampliar automaticamente o escopo.
Não houve serviços pagos, escrita de notícias no corpus ou novos agendamentos.

A tela ganhou a aba **Descoberta na web**: consultas enviadas, páginas de origem,
links declarados, resultado da validação e comparação amostral com editorias do menu.
Os filtros **Web: novos mecanismos** e **Web: falha na pesquisa** ajudam a revisar os
resultados. `/api/mapa/revisao-web?site=CHAVE` lê os detalhes locais, sem buscar na internet.
O download atualizado está em [`var/revisao-web-fontes/resultado.xlsx`](var/revisao-web-fontes/resultado.xlsx),
com as auditorias anteriores preservadas em abas separadas. CSVs, JSONs, backups,
consultas por domínio e verificações da tela estão na mesma pasta. A rodada está
encerrada, sem continuação automática; abrir a tela não dispara processamento.

## Rodada exploratória final das fontes sem confirmação

Rodada única concluída em 18/10/2026 nas **295 fontes únicas** correspondentes aos
**297 registros** sem auditoria anterior (duas duplicatas). Foram reaproveitadas pistas,
robots.txt, até três páginas conhecidas e caminhos usuais de mecanismos, sem filtro de data,
serviços pagos ou continuação agendada. Limites: 32 chamadas e 75 segundos por fonte,
12 XML por fonte, profundidade máxima dez e duas notícias amostrais; teto global de 15 minutos.
A exploração terminou em cerca de 6,5 minutos, com **2.000 chamadas** e nenhum erro interno.

| Resultado da rodada | Fontes |
|---|---:|
| XML confirmado | 31 |
| Acesso bloqueado, incluindo mecanismo declarado da Band | 147 |
| Restrições de robots | 19 |
| Redirecionamento para outro domínio | 19 |
| Falha de conexão/robots indisponível | 18 |
| Mecanismo não localizado na amostra | 55 |
| Limite local de execução | 6 |

Das 31 fontes com XML confirmado, **14** permitiram acessar notícia e **13** também
forneceram texto legível. O total combinado passou de 810 para **824 fontes** com notícia
acessível e de 777 para **790** com texto legível. Isso não comprova cobertura integral;
falhas e limites não significam inexistência de mecanismos. A Band está identificada como
**mecanismo declarado; acesso automático bloqueado**, com seu sitemap-news.xml registrado,
sem classificá-lo como XML validado ou notícia acessível.

O diretório oficial [Siga o G1 por RSS](https://g1.globo.com/tecnologia/noticia/2012/11/siga-o-g1-por-rss.html),
fornecido pelo usuário, acrescentou uma conferência finita de **70 links declarados**:
**69 funcionaram com itens**; um teve falha temporária de DNS. Os redirecionamentos antigos
`/dynamo/.../rss2.xml` foram resolvidos para os caminhos atuais; o inventário reutilizável
registra os destinos validados. Uma única conclusão verificou os 22 links da lista original
que ficaram sem tentativa na primeira janela, sem repetir os 48 confirmados nem ampliar
os 70 links. O retrato detalhado está em `var/conferencia-diretorio-g1/validacao.json`.

Os relatórios anteriores permanecem preservados. Os novos metadados de mecanismos foram
salvos no mapeamento, com backups das fontes afetadas; artigos não foram gravados no corpus.
O inventário e plano funcional foram regenerados offline. A tela inclui os resultados da
rodada, os bloqueios conhecidos e os caminhos encontrados no G1; uma nova leitura de XML
não apaga evidência anterior de notícia acessível. O download completo atualizado fica em
`var/exploracao-pendentes-final/resultado.xlsx` e inclui as auditorias anteriores.

Os padrões de XML definem como interpretar mecanismos, sem impor uma URL universal nem
comprovar que o arquivo lista todas as páginas do site. O leitor existente aceita RSS,
Atom e sitemaps sem exigir data; não houve troca de biblioteca nem implantação de coleta
contínua a partir das sugestões de código fornecidas pelo usuário.

## Limite de descoberta evidenciado pelo diretório do G1

A conferência de acesso valida caminhos conhecidos; não mede completude de editorias.
Uma fonte entre as 824 com notícia acessível pode ter muitos mecanismos ainda não
localizados. As rodadas executadas não incluíram busca web sistemática por cada fonte;
usaram sobretudo robots.txt, páginas conhecidas, dicas e caminhos usuais. O diretório
oficial do G1, encontrado pelo usuário, expôs essa limitação de descoberta.

No G1, o inventário funcional passou de 45 para **88 mecanismos** (editorias e regiões),
incluindo política, tecnologia, economia e educação; são 89 registros com uma falha
transitória de DNS preservada separadamente. Os 69 feeds funcionais do diretório foram
normalizados para seus destinos atuais e cruzados com o que já existia, sem contar
aliases como novas fontes. O material sobre protocolos XML não resolve essa etapa:
padronização do conteúdo não implica endereço universal, publicação no robots obrigatório
ou lista completa de páginas.

Uma próxima melhoria de método deve avaliar também as fontes já confirmadas: busca web
limitada por domínio por páginas oficiais de RSS/sitemaps, extração dos diretórios de
editorias e comparação com as seções publicadas. Esse processo precisa de limites de
consultas/páginas por fonte e resultado explícito, sem descoberta recursiva indefinida.
**Naquela rodada, a busca web sistemática ainda não havia sido executada.** Ela foi
realizada depois, na revisão limitada documentada acima, e não foi agendada para repetição.

## RSS/sitemaps funcionais para buscas

O critério principal é **XML acessível e interpretado com sucesso na última auditoria**,
independentemente da idade ou quantidade de itens. Entram resultados `fresh`, `slow`,
`undated`, `stale`, `archive` e `empty`, em RSS/Atom, sitemap ou news sitemap.
Atualidade dos itens e URL com data fixa são informações separadas, sem excluir mecanismos.
Uma URL com data fixa serve ao período indicado; sua existência não comprova um índice
estável para atualizações futuras. A classificação é baseada em evidência armazenada,
sem revalidar a internet ao abrir a tela.

Não entram como funcionais HTML, feeds de comentários, respostas inválidas, falhas/bloqueios,
links assinados, credenciais na URL ou mecanismos excluídos manualmente. Um RSS pode ficar
fora do prefixo editorial da fonte no mesmo host; os links de notícias respeitam o escopo.

A aba **RSS e sitemaps** inicia em **Todos os RSS/sitemaps funcionais**, enquanto a tela
abre na aba de acesso às notícias. O inventário funcional exibe resultado da leitura, idade dos
itens, leitura parcial, exemplos e marca de data fixa. Há filtros opcionais de atualidade;
esses filtros não definem o total funcional nem o plano principal. O CSV de download segue
o critério funcional. A aba de seções preserva declarações e correspondências observadas
com até cinco exemplos por página, distinguindo auditorias anteriores à página. Isso não
comprova cobertura integral da seção.

Após a rodada exploratória: **1.030 fontes com XML funcional** e **264** sem confirmação
funcional; 36.552 registros funcionais, ou 36.204 após deduplicação canônica por fonte no
plano. Esse resultado é separado das 824 fontes com acesso a notícias comprovado.

Retrato anterior à rodada exploratória de 18/10/2026: **999 fontes** com XML funcional, **295** sem leitura funcional
registrada. São 36.402 registros de mecanismos funcionais, ou **36.054** após deduplicação
canônica por fonte no plano. **997 fontes** têm mecanismo sem data fixa; duas só têm endereços
com data fixa identificados. As leituras gerais de referência são de 17/10; os RSS regionais do G1 foram validados em
18/10 nas rodadas fechadas descritas abaixo.
Os antigos totais 855/944 eram filtros por atualidade e não representam o total funcional.

Exportação offline, finita e sem coleta/agendamento:

```bash
scripts/bm.sh export-mechanisms --out var/mecanismos-busca
```

Arquivos principais:

- `plano-funcional.json`: todos os XML funcionais, compatível com `--discovery-plan`, com resultado
  temporal, data da auditoria, parcialidade e data fixa preservados.
- `hints-funcionais.csv`: todos os mecanismos funcionais para `--hints`.
- `mecanismos.csv`: inventário com `functional` e `fixed_date` separados do resultado temporal.
- `fontes.csv`: todas as fontes únicas, inclusive sem mecanismo funcional.
- `secoes-mecanismos.csv.gz` e `lacunas-secoes.csv.gz`: relações observadas e lacunas por página.
- `resumo.json` e `LEIA-ME.txt`: critérios e contagens.

Os antigos `plano-descoberta.json`/`hints-recentes.csv` e `plano-ampliado.json`/
`hints-complementares.csv` permanecem como seleções opcionais por atualidade. O plano padrão
do projeto e as estratégias do banco não são alterados. Na futura busca manual, use
`discover --discovery-plan var/mecanismos-busca/plano-funcional.json`; orçamento e
`--max-complements` continuam limitando a leitura por rodada. Esse comando faz coleta e não
foi executado nesta entrega. As buscas revalidam HTTP/conteúdo e respeitam robots/escopo.

APIs somente leitura: `/api/mapa/mecanismos?site=CHAVE&mode=functional` (padrão, paginada),
`mode=fixed_date|recent|supplement|review|all` (filtros opcionais) e
`/api/mapa/mecanismos.csv?site=CHAVE&mode=functional` (sem truncar; sem `site`, todo o catálogo).

## Exploração regional limitada: G1 e Folha

Em 18/10/2026, a seleção fechada de páginas já mapeadas identificou estruturas do G1 nas
27 UFs. Derivar um RSS dessas URLs foi uma hipótese, seguida de validação do XML: os
27 endereços responderam como feeds com itens recentes. Cada um listou 100 URLs; entre
93 e 100 pertenciam ao prefixo da UF correspondente. Essa correspondência não comprova
que todas as notícias ou subseções do estado estejam presentes no feed.

Os 27 RSS foram incorporados às auditorias da fonte nacional `g1.globo.com`, preservando
as auditorias existentes e as fontes estaduais já cadastradas. O catálogo não recebeu
27 fontes artificiais novas. Eles aparecem na tela do G1 e no plano funcional.

A Folha apresentou páginas temáticas sobre lugares e dois RSS editoriais interpretáveis
(`cotidiano` e `poder`, vazios nessa leitura), mas não foram confirmadas 27 edições estaduais
independentes. Não se presume a mesma estrutura para todos os veículos.

A rodada teve 31 chamadas à pilha HTTP em 31,7 segundos, com teto de 31 chamadas/120 segundos,
intervalo por host e robots, sem retries, expansão automática ou leitura de artigos.
Evidências: `var/exploracao-regional/rss-estaduais.csv`, `correspondencia-ufs.json`,
`auditorias.json`, `plano-regional.json` e `hints-regionais.csv`. O arquivo
`g1-auditorias-antes.json` preserva a fotografia anterior à incorporação; o procedimento
fechado é `var/exploracao-regional/verificar_rss.py`.

## Conferência encerrada de 20 veículos

Rodada fechada em 18/10/2026: 16 veículos brasileiros e quatro estrangeiros, selecionados
por links de mecanismos declarados ou padrões de RSS/sitemap já válidos em seções equivalentes
do mesmo host. A triagem offline descartou combinações de blogs/hosts/áreas de serviço,
sem fazer chamadas para elas. Não foram testadas combinações indiscriminadas de estados.

A rede terminou em **213,3 segundos**, com **501 requisições físicas**, incluindo robots e
redirecionamentos. Limites: 20 veículos, 30 chamadas por veículo, 900 segundos globais,
quatro workers e intervalo por host. Não houve retries ou nova rodada automática.
Os limites de chamadas, redirects e prazo foram verificados também com transporte simulado.

Resultado: **300 mecanismos funcionais acrescentados em 18 veículos**, incluindo 70 vazios.
**Balobaki Check, Malawi24 e Ukrinform** passaram de nenhuma auditoria funcional para mecanismos
confirmados. Sul21 e Guineenews não acrescentaram mecanismo funcional nesta seleção.
Auditorias válidas anteriores foram preservadas, com backup por veículo antes das inclusões.

Na comparação limitada a até 500 páginas por fonte e cinco exemplos por página, **239 páginas
de seção ganharam correspondência** e houve **719 correspondências de links adicionais**.
Os novos documentos também listaram **12.815 URLs adicionais** sob escopo, após filtro de
formato, em comparação com os documentos antigos armazenados. Isso não representa artigos
lidos, publicação necessariamente recente ou prova de cobertura exclusiva: os retratos têm
datas diferentes. O corpus não foi alterado, e não houve LLM, serviço pago ou agendamento.

Entregas em `var/conferencia-20-veiculos/`: `resultado.xlsx`, `resultado-fontes.csv`,
`mecanismos-validados.csv`, `hints-validados.csv`, `plano-validado.json`, `resumo.json` e
`urls-adicionais.csv.gz`. Os leitores reais de plano/hints, as contagens da planilha e as
regras manuais foram conferidos. A seleção, os procedimentos executados, as auditorias e
backups gzip por fonte permanecem na mesma pasta. O plano funcional global e a tela foram
atualizados; não existe continuação agendada desta conferência.

## Profundidade de expansão de sitemaps

O explorador de descoberta agora segue índices de sitemaps aninhados, com profundidade
padrão máxima de **10 níveis abaixo da entrada** (entrada = nível zero). O parâmetro Python
`max_sitemap_depth` de `explore_site` permite configurar essa profundidade. URLs já visitadas
não são requisitadas novamente durante o caminhamento, inclusive em ciclos.

A profundidade não substitui o orçamento de arquivos: `--max-sitemap-fetches` continua
limitando o total lido (padrão seis). Uma cadeia com entrada mais dez filhos precisa de pelo
menos 11 leituras disponíveis; use orçamento compatível quando for necessário atingir o
nível dez. Os pontos de entrada declarados mantêm prioridade, e a descida dos índices
aninhados não abre uma exploração sem limite. Datas e `lastmod` continuam ordenando o
caminhamento da descoberta; a auditoria de acesso não descarta notícias pela data.

Testes cobrem índices aninhados, ciclo entre índices, parada no nível dez e orçamento
independente de arquivos; a descoberta incremental existente continua com os mesmos
checkpoints e gancho de `lastmod`.

## Auditoria de acesso a notícias pelos sitemaps

Concluída em **2026-10-18**, sem filtro de data de publicação. Universo fechado:
**1.814 URLs de entrada distintas**, com **1.856 associações a 822 fontes** do catálogo.
RSS ficou fora desta rodada. A leitura incluiu índices e sitemaps filhos; os totais abaixo
contam as entradas distintas, sem inflar a contagem pelos filhos percorridos.

| Evidência observada | Sitemaps de entrada | Fontes |
|---|---:|---:|
| Pelo menos uma página de notícia acessível | **1.374** | **656** |
| Também com texto legível na amostra | **1.286** | **623** |

Em **1.742** entradas foi possível interpretar XML. Foram avaliadas **2.165 associações
de amostras**, correspondentes a **1.692 URLs de notícias distintas**. A notícia precisa
retornar HTML, ter título e passar pelas verificações de página de notícia, erro suave,
desafio e redirecionamento. Texto legível exige também aprovação dos critérios de extração;
isso não garante o texto integral. Das fontes com notícia acessível, **33** não tiveram
texto legível confirmado; outras **166** ficaram sem uma página de notícia confirmada.
Falha ou ausência de confirmação na amostra não demonstra impossibilidade de acesso ao site.
O índice principal de O Globo foi confirmado com notícia acessível e texto legível.

A verificação usou profundidade máxima **10** (entrada no nível zero), até **12 XML por
entrada**, **duas tentativas de notícia por associação** e **40 chamadas por entrada**,
respeitando robots.txt. Houve 20 minutos iniciais e uma única conclusão das 189 entradas
que ainda não tinham sido consultadas, com teto adicional de cinco minutos e sem repetir
as entradas concluídas. Resultado: **5.271 chamadas HTTP**, cerca de **22,4 minutos** de
execução, nenhuma entrada pendente de início e nenhum erro interno. A profundidade efetivamente
observada chegou a três; limites e falhas permanecem detalhados por associação no relatório.
Nenhuma notícia foi gravada no corpus e nenhum agendamento foi criado.

Planilha com abas Fontes, Sitemaps, Referências e Critérios:
[`var/auditoria-acesso-sitemaps/resultado.xlsx`](var/auditoria-acesso-sitemaps/resultado.xlsx).
Os CSVs, links de exemplo, avaliações de amostras e resumo JSON estão na mesma pasta.
Este é um retrato amostral de acesso, sem garantia de cobertura integral ou acesso futuro;
não substitui o inventário de mecanismos funcionais, que também inclui RSS.

## Auditoria de acesso a notícias pelos feeds RSS/Atom

Concluída em **2026-10-18**, sem filtro de data de publicação. O inventário contém
**33.949 URLs distintas de feeds**, em **829 fontes** (34.546 associações fonte–feed).
Esta foi uma amostra por fonte: até **três feeds selecionados por fonte**, priorizando
links de exemplo e itens já observados, sem rejeitar feeds antigos, e até **dois links
de notícia por feed**. A verificação encerra a fonte quando confirma texto legível;
portanto, os números abaixo não representam uma auditoria de todos os feeds ou seções.

| Evidência observada | Apenas RSS/Atom | União com sitemaps, sem duplicar fontes |
|---|---:|---:|
| Fontes com página de notícia acessível | **653** | **810** |
| Fontes também com texto legível | **617** | **777** |

O RSS acrescentou **154 fontes** com notícia acessível ao resultado da auditoria de
sitemaps. Em 36 das fontes confirmadas por feed, não houve texto legível confirmado;
outras **176** ficaram sem notícia acessível confirmada nesta amostra. Resultado negativo
amostral não demonstra impossibilidade de acesso ao site ou aos feeds não testados.

Foram consultadas **1.034 URLs distintas de feeds** (1.039 associações fonte–feed),
com **1.148 associações de amostras de notícias**, correspondentes a **1.097 URLs distintas**.
Das URLs de feeds consultadas, **689** levaram a uma notícia acessível e **616** também
forneceram texto legível em pelo menos uma associação. O XML foi interpretado em pelo
menos um feed de **787 fontes**. RSS, Atom e RDF foram aceitos; não se seguiu paginação
nem se buscaram mecanismos novos. As páginas de notícias passaram pelos mesmos critérios
de identificação, erro suave, desafio, redirecionamento, extração e paywall da auditoria
de sitemaps. Texto legível não garante texto integral.

A rodada respeitou robots.txt, intervalo por host, limites de 40 chamadas por fonte,
90 segundos para iniciar novas requisições por fonte e teto global de 20 minutos/15 mil
chamadas. Terminou em cerca de **8 minutos**, com **3.075 chamadas HTTP** e nenhum erro
interno. Todas as 829 fontes tiveram ao menos um feed tentado; duas atingiram limite
local e uma das entradas selecionadas ficou sem tentativa. Esses limites são registrados
no relatório, assim como os feeds não testados por parada antecipada ou limite de três.
Não houve coleta contínua, gravação no corpus, chamadas pagas ou novos agendamentos.

Planilha: [`var/auditoria-acesso-rss/resultado.xlsx`](var/auditoria-acesso-rss/resultado.xlsx).
Abas: **Consolidado** (1.294 fontes únicas do catálogo e evidências RSS/sitemap),
**Fontes RSS**, **Feeds verificados**, **Inventário** (inclui não testados) e **Critérios**.
CSVs, links de exemplo, avaliações de amostras, plano fechado e resumo JSON estão na mesma
pasta. A união combina esta rodada com o retrato anterior dos sitemaps; não constitui
medida de cobertura integral dos sites nem garantia de acesso futuro.

## Fechamento da recuperação estrutural

Retrato consolidado em 2026-10-18T11:44:04.169802+00:00:

| Situação estrutural observada | Fontes |
|---|---:|
| Navegação identificada examinada | 347 |
| Mapa parcial | 441 |
| Acesso restrito | 227 |
| Estrutura não confirmada | 279 |

Foram saneados 26694 palpites sem evidência de seção/navegação que já
haviam sido refutados por 404/410, recurso não HTML ou retorno à entrada. Links de menus e
seções confirmadas não foram descartados por essa regra. A evidência anterior foi preservada
em `var/recuperacao-mapeamento/candidatas-refutadas.jsonl.gz`. Saneamento não é ganho de cobertura.

A seleção congelada tinha 767 fontes. 342 receberam
chamadas; 425 não iniciaram por esgotamento do prazo global.
Houve 1842 chamadas à pilha HTTP e 576 páginas
que passaram de falha/pendência para leitura válida. As novas leituras produziram
202 confirmações de tipo seção, incluindo entradas.
O inventário consolidado tem 27947 registros de seções confirmadas
(excluindo entradas) e 953 fontes com exemplos observados em páginas lidas.

A exploração teve teto de 900 segundos, quatro workers, 20 chamadas e 40 segundos por fonte.
Uma tentativa por URL selecionada, sem retry interno; robots e intervalo por host preservados.
A entrada relida pôde acrescentar até duas leituras de navegação, sem expansão recursiva.
O reconhecimento de menus passou a considerar identificadores CSS explícitos; isso gera
candidatas, não confirmação de tipo. Se o menu atual aponta um palpite refutado, ele é reaberto.

Entrega atual: `var/recuperacao-mapeamento/resumo-fontes.xlsx`,
`var/recuperacao-mapeamento/resultado-final.xlsx` e `var/recuperacao-mapeamento/conclusao.json`.
As planilhas completas anteriores permanecem como fotografias históricas. A tela consulta
o estado atual. A etapa está fechada, sem nova rodada automática, LLM, serviços pagos,
coleta contínua ou gravação no corpus.

## Evolução futura: manutenção periódica do mapa

Planejada, sem agendamento ou implementação nesta entrega. Uma execução periódica terá
orçamento próprio de chamadas, tempo, páginas e profundidade. Começa pelas entradas e menus
conhecidos, compara o retrato anterior e prioriza caminhos novos ou alterados.

- Usa URLs normalizadas, histórico de visitas e proteção contra ciclos; cada execução termina
  pelo orçamento ou pela seleção fechada, mesmo se houver novos caminhos a examinar.
- Separa editorias/subseções de artigos, serviços e taxonomias; respeita escopo e robots.
- Registra novos caminhos como candidatas, com origem e evidência; confirma o tipo por leitura.
- Preserva falhas com espera antes de outra tentativa; ausência em um menu não prova remoção
  global da seção e não elimina evidências antigas automaticamente.
- Entrega diferenças observadas: caminhos novos, confirmados, alterados e pendentes. Não calcula
  um percentual de cobertura total sem uma referência externa confiável do próprio site.

Essa manutenção é independente do fechamento do inventário atual e não reabre sua etapa
indefinidamente nem implica coleta contínua de notícias.

## Encerramento anterior do mapeamento estrutural

O escopo desta entrega é o mapa de editorias, subseções e navegação das 1.294 fontes.
A etapa foi encerrada em 2026-10-18T01:58:59.890213+00:00, após auditoria do catálogo e uma rodada final
limitada de exploração. Não há novos lotes ou retomada automática.

| Situação estrutural | Fontes |
|---|---:|
| Navegação identificada examinada | 94 |
| Mapa parcial | 713 |
| Acesso restrito | 227 |
| Estrutura não confirmada | 260 |

A rodada consultou 32 fontes, com 596 chamadas à
pilha HTTP em 550.8 segundos. Aplicou tetos de 100 chamadas e 120 segundos por
fonte, quatro fontes em paralelo e 1.200 segundos globais. As chamadas em andamento terminam
pelos timeouts existentes; exportação e verificação ficam fora do prazo de exploração.
Retentativas e redirecionamentos internos são contabilizados separadamente no transporte.
As restrições conhecidas na entrada foram preservadas. As candidatas da navegação têm
prioridade sobre caminhos inferidos; não houve nova expansão por sitemaps/feeds.

O inventário final contém 673914 registros de páginas e 27774 registros de seções confirmadas por
metadados, 243854 candidatas lidas sem confirmação de tipo e 25030
candidatas pendentes preservadas. Navegação identificada examinada exige entrada válida,
evidência estrutural e ausência de candidatas pendentes/falhas reconhecidas; não comprova
que todas as seções existentes foram descobertas. Falhas, tipos incertos e restrições constam
como lacunas. Relações de descoberta por link não declaram sozinhas hierarquia editorial.

Entrega: `var/mapeamento-final/resumo-fontes.xlsx` traz a visão curta das 1.294 fontes.
`var/mapeamento-final/mapa-estrutural-final.xlsx` contém o detalhamento completo:
inventário completo, seções e candidatas, relações observadas, lacunas finais, artigos/serviços/
taxonomias identificados, possíveis duplicatas e critérios. Cada aba também tem CSV gzip
integral. `var/mapeamento-final/encerramento.json` registra métricas e verificações;
`var/mapeamento-final/RESUMO.md` traz o balanço curto. As evidências e planilhas anteriores
foram preservadas. Os procedimentos utilizados estão em `var/mapeamento-final/procedimentos/`.

Esta etapa tratou somente do mapeamento estrutural, sem descoberta incremental, LLM, serviços
pagos, gravação no corpus ou alteração do cron. Os fluxos de coleta documentados abaixo são
funcionalidades anteriores e não fazem parte desta entrega.

## Decisões

- **Descoberta em cascata, da mais barata à mais cara:** sitemap gratuito, feed RSS/Atom gratuito e,
  só para quem bloqueia, sitemap pelo Bright Data. (A escolha inicial era "só sitemap"; a medição
  de 2026-10-15 mostrou que o feed resgata de graça ~23% das fontes sem sitemap gratuito.)
- **O corpus é por entidade, não por adversidade:** entra toda matéria com ao menos uma pessoa ou
  organização identificável (adversa ou não); só se descarta o que não tem quem identificar. O fato
  adverso é uma marca (`finding`, temas), nunca um filtro. O veredito por entidade e tema
  (`match_nome`, `alerta`) continua no Treatment, na hora da DD, e a matéria sem tema adverso é
  achada no KyX pela categoria "Diversos" (palavra-chave `diversos`).
- **Sem triagem por manchete.** Havia L0 (forma da URL), L1 (Naive Bayes) e uma triagem por LLM;
  com o critério por entidade a triagem descartava ~2% das URLs e custava mais do que poupava, e o
  L1 nunca entrou no fluxo. Sobrou só o L0, que é barato e certeiro. A decisão de verdade é a
  análise do texto.
- **Estratégia por fonte, descoberta e lembrada (`strategy/`):** a cascata estática (sitemap, feed,
  pago) não sabe qual endereço serve cada fonte e relê arquivos inúteis (Le Monde: 105 mil URLs de
  arquivo no lugar do sitemap de notícias de ~250 itens). `probe-sources` baixa os candidatos
  (dicas, `robots.txt`, caminhos usuais, links da home), mede o que cada um publica agora (itens
  das últimas 48 h, com título e data), descarta arquivo, lista defasada e HTML, pontua com regras
  explícitas e grava um principal e uma reserva de outro tipo. O CSV de levantamento é só dica: é
  uma fotografia de uma data e de uma rede. Fatia 1 (sondar e gravar) e fatia 2 (o `discover` usa o
  principal e a reserva, com a cascata de sempre de plano B) e fatia 3 (reavaliação
  automática por saúde, idade e espera crescente; modelos para URLs com data) feitas. O `run`
  reavalia só as fontes na vez antes de cada descoberta (`probe-sources --due`). Cobertura do site
  (seções, união das fontes, complementos) e planilha de conferência (`export-strategies`) feitas;
  a descoberta lê principal/reserva + complementos, com deduplicação, checkpoints e orçamento
  compartilhado para as chamadas gratuitas. Correções à mão por fonte
  (`estrategias_manuais.csv`: excluir ou forçar um endereço) e regras vindas da conferência de sites
  reais (listagens de tags fora, nova tentativa para o que o site declara, índices de várias
  famílias, índices de feeds) feitas. Para a fonte sem sitemap nem feed a estratégia é a varredura
  de listagens (home e páginas de seção, com novas seções achadas nas seções; sem datas, nota mais
  baixa); a descoberta ainda não a lê.
- **Um critério, em um lugar** (`criteria.py`): a matéria entra se a análise **leu o texto** e achou
  **entidade identificável**. Evidência e corpus olham só para ela (a regra existe em Python e em
  SQL, com teste de concordância).
- **Um comando, um agendador:** `run` encadeia as etapas (cada uma é um subcomando, retomável pelo
  estado); o cron chama `scripts/run.sh`. Uma etapa que falha não impede as seguintes.
- **HTTP direto primeiro:** o HTML baixado já é a evidência; Bright Data (pago) só como fallback e
  só para fato adverso.
- **Portas e adaptadores:** cada etapa é uma interface; a escrita no corpus (`CorpusWriter`) grava
  direto no Postgres (sem a fila do KyX, cujo consumer exige um `RunContext`).
- **HTTP síncrono + threads:** o trabalho é só I/O e cada host é atendido uma requisição por vez
  (`ThrottledFetcher`, trava por host); evita a complexidade de `asyncio` na POC.
- **Leitura defensiva de XML:** sitemaps são conteúdo de terceiros — sem entidades externas, sem
  rede, e limite de 50 MB descomprimido (gzip bomb).
- **Estado em Postgres** (`midias_crawler_*`, o mesmo banco do corpus): URLs vistas, saúde por
  site, análises, evidências e gravações. Começou em SQLite e migrou em 2026-10-16 (o DDL é a fonte
  do esquema e vira migration do powerdd-backend).
- **Backfill histórico:** fora do escopo inicial; pensado depois, com janela finita.

## Fases

### Histórico do levantamento de cobertura

Os levantamentos em `var/estrategias.xlsx` e nas planilhas auxiliares são a base existente.
Percentual sobre a união de candidatos recentes não comprova cobertura das seções do site.
O trabalho segue esta ordem:

A varredura aberta foi encerrada em 2026-10-17 por decisão do usuário. A prioridade passou
a ser consolidar os checkpoints e complementar lacunas de cobertura, mantendo caminhos não
explorados e restrições explícitos. O encerramento não significa que todas as seções foram
descobertas. As próximas execuções têm orçamento total por fonte e prazo global, além dos
limites por lote: 200 chamadas à pilha HTTP e 180 segundos por fonte, 1800 segundos para a
exploração do conjunto, configuráveis. Páginas, robots e mecanismos compartilham o saldo.
O prazo é conferido antes da próxima chamada; requisições em andamento usam os timeouts
existentes, e exportação fica à parte. O inventário permanece retomável ao atingir o teto.
`--report-only --priorities-only` classifica as fontes para complementação: ausência de
mecanismo recente confirmado, lacunas nas amostras, acesso/estrutura a revisar e amostras
correspondentes. Isso não altera a estratégia de coleta nem inicia novas buscas.

O piloto dirigido posterior conferiu 20 fontes em 257 segundos/523 chamadas: 15 tiveram
ganho e 16.508 URLs distintas das mesmas amostras ganharam correspondência, sem perda nas
anteriores. Foram encontrados 42 mecanismos recentes novos; 68 endereços recentes, incluindo
reavaliações, ficaram como dicas reutilizáveis. As prioridades globais passaram a 152/818/280/44.
A descoberta isolada em cinco fontes validou união e deduplicação. A seleção orientada a
sitemaps de notícias/feeds datados acrescentou 345 candidatas com publicação declarada nas
últimas 48 horas, além dos principais escolhidos para o piloto. Índices amplos também
acrescentaram URLs, mas trouxeram muito arquivo e atingiram o orçamento em uma segunda leitura.
O ganho deve ser medido com recência e tipo de data, mantendo as limitações de amostra e
orçamento: `lastmod` não comprova publicação e URLs aceitas por L0 não são artigos confirmados.
Resultados: `var/piloto-resultados.xlsx` e `var/piloto-validacao-final.json`.
Os pilotos isolados não alteraram estratégias, estado incremental, corpus ou cron e não
utilizaram serviços pagos. A integração automática foi implementada depois: lê complementos
gravados e o plano validado das cinco fontes em `data/plano_descoberta.json`, sem reabrir
o inventário. O principal usa a reserva somente se falhar; complementos acrescentam URLs
mesmo se ele servir. A união canônica preserva publicação declarada e checkpoints dos filhos;
falhas parciais não descartam os demais resultados. O padrão limita o conjunto a três
complementos, 40 chamadas gratuitas e 120 segundos para iniciar chamadas por fonte, incluindo
robots e fallback gratuito (redirecionamentos/retentativas internos ficam à parte). Os
limites existentes por mecanismo continuam valendo. A origem `combined` exige SQL 007;
sem essa migração, o comando para antes do HTTP. Regras manuais, escopo e robots se aplicam
também aos complementos. Catálogos personalizados exigem plano explícito; `--no-strategy`
desliga as escolhas e o plano, e `--max-complements 0` desliga a união.

A validação da integração pelo CLI real usou apenas HTTP gratuito e estado incremental em
um schema descartável do banco de testes: cinco fontes, duas leituras, todas sem falhas ou
leituras parciais. A primeira reuniu 6.053 URLs candidatas, 2.425 adicionais dos complementos;
352 dessas adicionais tinham publicação declarada nas últimas 48 horas. A segunda registrou
zero URLs novas e pulou dois filhos com checkpoint inalterado. Foram 68 chamadas HTTP de
transporte. Isso valida a integração e a deduplicação, sem comprovar cobertura total ou
relevância das notícias. Relatórios: `var/integracao-complementos-piloto.xlsx` e
`var/integracao-complementos-piloto.json`. A migração 007 foi aplicada no Postgres local,
preservando os 180 registros de rodadas existentes; o corpus e o cron não foram alterados.


O lote dirigido seguinte conferiu outras 20 fontes em 125,7 segundos/314 chamadas,
com 231 leituras/reavaliações e 31 mecanismos novos recentes. Sete fontes passaram a ter
mecanismo recente confirmado (42 endereços exportados). Oito fontes tiveram ganho nas
mesmas amostras congeladas: 2.288 URLs únicas por fonte, sem perda das correspondências
anteriores. As prioridades globais ficaram em 145/825/280/44. Treze fontes permanecem para revisão; ColombiaCheck ganhou correspondência,
mas continuou sem confirmação de mecanismo recente. O lote não explorou novas seções nem
alterou estratégias de coleta, estado incremental, corpus ou cron. Os mecanismos exportados
seguem como dicas para validação incremental, sem promoção automática ao plano. Resultados:
`var/lote-02/resultados.xlsx` e `var/lote-02/validacao-final.json`. Outro lote foi apenas
selecionado em `var/lote-02/proximas-fontes.txt`, sem execução.

1. Inventário persistente, completo e retomável das páginas candidatas a seção: primeira
   implementação em `map-sites`, com checkpoint por página, origem, fila e falhas explícitas
   (`004_create_midias_crawler_section_pages.sql`). Exporta catálogo, páginas, mecanismos
   encontrados e amostras sem truncar o inventário. A estratégia anterior aparece como referência.
   A aba Revisão distingue falhas das entradas: proibição explícita de robots, falha ao ler a
   política, limites, DNS, TLS, rede, conteúdo e redirecionamento fora do escopo. Preserva o estado
   e o motivo e não dispara tentativas ou mudanças no catálogo automaticamente.
   Três falhas consecutivas de conexão na mesma origem, dentro do mesmo lote de páginas
   ou passagem de auditoria, suspendem somente aquela origem, mantendo suas páginas/mecanismos
   não tentados pendentes e o motivo explícito. Demais hosts continuam sendo mapeados e
   auditados, inclusive prefixos novos apontados pelos mecanismos. A suspensão é mantida entre
   lotes e passagens da mesma execução; outra execução pode retomar a origem.
2. Ampliar o reconhecimento de seções pela navegação e cruzar a estrutura com sitemaps; distinguir
   editorias, taxonomias e serviços. A heurística de URL atual ainda é limitada. Primeira
   classificação por evidência implementada (DDL 006): schemas da própria página, com proteção
   contra artigos destacados de outras URLs e metadados conflitantes; tipo desconhecido fica
   para revisão. Registra navegação explícita, que passa à frente dos palpites de URL na fila.
   Matérias e serviços reconhecidos não expandem a fila. A navegação também permite reconhecer
   rotas de seção com extensão HTML/PHP, antes ignoradas.
   Diretórios de tópicos/pessoas (`topic`, `topics`, `topics-list`, `themen`, `tagg`, `amnen`,
   `persons`) são taxonomias e ficam
   preservados para revisão, inclusive quando já estavam pendentes antes dessa classificação.
   A entrada cadastrada mantém prioridade e não é excluída por esse caminho.
   A expansão relativa com três segmentos iguais consecutivos, observada em `exit.al`
   (`category/category/category/...`) e `monde-diplomatique.fr` (`index/index/index/...`),
   é suspensa como possível ciclo, mantendo registros para revisão, sem teto geral de profundidade.
   O modo contínuo também cruza caminhos de matérias em mecanismos lidos com o inventário:
   prefixos ainda desconhecidos entram como candidatas `mechanism_path`, respeitando escopo,
   taxonomias, serviços e datas; seguem sem confirmação até leitura. A exploração e a auditoria
   se repetem quando esses caminhos revelam páginas ou mecanismos novos.
3. Validar e associar sitemap/feed/listagem a cada seção; comparar as matérias da seção com os
   mecanismos encontrados. Primeira conferência de amostras implementada em `--validate-sources`:
   lê mecanismos com orçamento incluindo robots/filhos, guarda as URLs em tabela separada (005)
   e exporta correspondências e lacunas. Não declara cobertura total a partir da amostra.
   Paginação HTML em `/index/feed/pagina-N` não é índice de feeds: não expande o histórico,
   mesmo que tenha sido classificada como índice em uma execução anterior. A correção preserva
   URLs observadas, custos e datas; XML válido nesse mesmo padrão continua sendo avaliado.
   Links a comentários são identificados antes de remover fragmentos para comparar URLs:
   feeds só de comentários ficam inválidos para cobertura, e feeds mistos não contabilizam
   esses links. As observações originais permanecem disponíveis para revisão.
   A aba Complementos sugere mecanismos pelo ganho incremental nos links observados por seção,
   preservando datas, leituras parciais e lacunas. Reutiliza um índice de URLs canônicas por fonte
   no relatório. A seleção é heurística e não muda a estratégia de coleta automaticamente.
   Manter seções sem publicação recente no inventário.
4. Conferir um piloto de estruturas diferentes e depois ampliar aos 1.294 registros, em lotes.
5. **Implementado:** a descoberta consome o conjunto validado, deduplica as candidatas e
   conserva as regras incrementais, os checkpoints e os limites por fonte. A ampliação para
   novas fontes permanece dirigida pelas prioridades; não há varredura aberta automática.

O inventário e a coleta são comandos independentes. `ok` numa página significa leitura, não
cobertura validada. Esgotar o orçamento deixa fila pendente; esgotar a fila reconhecida também
não autoriza afirmar que todas as seções do site foram descobertas. A implementação inicial
não muda o cron nem inicia coleta/enriquecimento/corpus.

| Fase | Entrega | Critério de aceite |
|---|---|---|
| 0. Esqueleto | Projeto, catálogo de fontes | pytest/ruff/mypy verdes; 1294 únicas |
| 1. Viabilidade | Parser de sitemap + relatório por site | Relatório dos 1294; amostra de 20–30 |
| 2. Descoberta incremental | Estado + corpus + backlog + filtro L0 + leitura parcial + cascata sitemap/feed/Bright Data (**feito**); medir volume e latência reais com as rodadas agendadas (pendente) | 2ª rodada sem URLs repetidas |
| 3. Triagem | ~~Triagem por manchete~~ (**removida** em 2026-10-16: ver Decisões); fica o filtro L0 | — |
| 4. Extração, análise, evidência | Texto por HTTP direto, análise de contexto (OpenRouter) e evidência com reaproveitamento validado do HTML (**feito**, armazenamento local); gravação direta no corpus Postgres (**feita**, `corpus-sync`, testada contra o banco local); S3 só com cliente falso | Compatível com `midias_ia_article*` |
| 5. Avaliação | Rotulagem humana ("entra no corpus?") e comparação com a busca ao vivo | Precisão e recall medidos; cobertura e custo por artigo |

## Estrutura

```
src/buscador_midias/
  domain/      modelos puros
  sites/       catálogo de fontes
  net/         fetcher educado (limite de tamanho, intervalo por host)  [fase 1]
  discovery/   robots → sitemap/feed (cascata) → relatório, filtro L0
  strategy/    sondar candidatos, medir, pontuar e escolher a estratégia de cada fonte
  criteria.py  o critério do corpus (Python e SQL)
  extraction/  artigo: texto, data, idioma                    [fase 4]
  analysis/    cliente OpenRouter, entidades, temas, resumo   [fase 4]
  evidence/    porta de armazenamento                         [fase 4]
  corpus/      CorpusWriter (Postgres), mapeamento, sync      [fase 4]
  state/       Postgres (midias_crawler_*) e migração do SQLite antigo  [fase 2]
```

## Regras de trabalho

TDD, SOLID, tipagem estrita (mypy strict), docstrings em todos os métodos públicos, testes sem
rede. Respeitar robots.txt e limitar a 1–2 requisições simultâneas por domínio.
