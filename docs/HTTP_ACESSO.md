# Transporte HTTP da busca de notícias

O leitor compartilhado acessa XMLs e notícias diretamente, sem robots.txt. A partir
de 25/10/2026 reutiliza conexões e sessões públicas por fonte. O mecanismo continua
responsável por interpretar RSS/sitemap; sessão HTTP é um recurso do transporte.

## Sessões e conexões

`catalog/http_session.py` mantém até 32 conexões ociosas por processo, expirando em
30 segundos. Uma conexão sai do pool durante o uso e só retorna após consumo
completo da resposta e confirmação de keep-alive. Falhas descartam a conexão;
nenhuma reconexão/repetição é feita escondida da contagem de tentativas.

Cookies recebidos publicamente são mantidos em memória, com domínio, caminho,
Secure e expiração aplicados por CookieJar. São isolados pelo domínio da fonte,
limitados a 128 fontes e 128 cookies por fonte. Não são importadas sessões do
navegador ou credenciais externas. Valores dos cookies não são registrados nos
relatórios. Encerramento do processo elimina as sessões.

Domínio/IP público, IP fixado na conexão, Host/SNI e TLS continuam validados.
Conexões reutilizadas conservam o IP público já validado. O transporte mantém
300 ms entre inícios de acesso ao mesmo hostname no processo, além das travas
entre consumidores e dos limites individuais de tempo, tamanho e requisições.

## Falhas e nova tentativa na ingestão

- Ciclos e excesso de redirecionamentos encerram a tarefa sem repetir a cadeia.
- 400, 401, 403, 404, 405, 410, 451 e 501 não são repetidos automaticamente. Um 403
  que atualizar cookies públicos da sessão pode ser tentado novamente, dentro do
  mesmo orçamento de até três tentativas.
- Timeouts, DNS temporariamente indisponível e erros transitórios continuam com
  espera crescente e até três tentativas da tarefa.
- 429 e 503 respeitam Retry-After válido em segundos ou data HTTP. Em 429 sem esse
  cabeçalho, aplica-se uma espera mínima de 60 segundos. A próxima tentativa não
  antecipa a espera normal da fila nem o prazo informado pelo servidor.
- `midias_manager.ingest_host_cooldowns` conserva a pausa do hostname no PostgreSQL.
  Publicadores evitam enviar outras tarefas do host durante a pausa; tarefas já
  enviadas conferem o prazo antes da rede e são adiadas sem gastar tentativas.

A espera é coordenada entre as filas HTTP da ingestão. O leitor do gerenciador e
Saúde das fontes também reutiliza sessões e respeita pausas em memória no próprio
processo; seus orçamentos de teste continuam separados dos da ingestão.

Os resultados registram requisições, conexões reutilizadas e quantidade de envios
de cookies, sem seus valores. O limite de três tentativas não se multiplica por
uma segunda camada de retries no cliente HTTP. Bright Data segue desativado na
busca de notícias. O transporte não usa navegador automatizado.

## Amostra reproduzível

```bash
PYTHONPATH=.:src .venv/bin/python scripts/probe_http_sessions.py
```

O comando lê seis URLs públicas, até duas vezes e no máximo três requisições
físicas por URL. Grava apenas metadados em `var/gerenciador/http-sessoes/`, sem
alterar cadastros, iniciar uma rodada ou chamar IA.

Na amostra de 25/10/2026, NeoFeed, Correio Braziliense e Brasil 247 retornaram XML
com notícias; as quatro segundas leituras bem-sucedidas reutilizaram conexões
(incluindo o XML vazio da Folha). Poder360 foi encerrado após uma chamada por
ciclo de redirecionamento. O RSS do g1 continuou retornando 503, e o sitemap da
Folha retornou XML vazio. A amostra comprova o transporte observado; não garante
que erros de acesso externos desapareceram nem mede uma rodada completa.

## Reteste dos excluídos originais com o transporte atualizado

`scripts/recover_excluded_sessions.py` seleciona no PostgreSQL as exclusões
lógicas automáticas que restam na lista histórica de 2.921 links. Exclusões
manuais, sites removidos e links já restaurados ficam fora da seleção. O manifesto
congela IDs, URLs, revisões dos cadastros e versões dos mecanismos.

A rodada de 25/10/2026 selecionou 2.705 associações de 116 fontes, com 2.696 URLs
únicas. Cada URL raiz é recebida uma vez por tentativa e o resultado é reaproveitado
entre associações equivalentes. São até três tentativas, um trabalho por hostname,
12 trabalhadores, 12 segundos por tentativa e respeito a Retry-After. O acesso às
raízes tem prazo total de 25 minutos: esperas maiores e itens não encerrados nesse
prazo são registrados como adiados, não como falhas definitivas.

A segunda fase reutiliza os XMLs preservados nesta rodada, verificando SHA256,
seleciona o mecanismo correspondente e executa o teste textual do gerenciador.
Não filtra a data da notícia. São até três artigos por amostra, até três acessos
por endereço, 24 requisições e 40 segundos por teste; o término total da rodada
é limitado a 50 minutos desde a criação do manifesto. Corpos públicos pequenos
bem-sucedidos podem ser reutilizados entre testes da mesma fonte. Nenhuma análise
de IA, captura de navegador ou chamada ao Bright Data é realizada.

Todos os resultados são arquivados em `link_test_results`; só há restauração e
ativação quando XML e notícia textual foram comprovados. A aplicação reutiliza
as mesmas transações, revisão de cadastros e eventos de recuperação anteriores,
sem alterar intervalos de busca ou apagar evidências antigas. Pais inativados
automaticamente só são reativados se houver um filho aprovado.

Com as credenciais PostgreSQL no ambiente:

```bash
PYTHONPATH=.:src .venv/bin/python scripts/recover_excluded_sessions.py
```

Os artefatos ficam em `var/gerenciador/reteste-http-melhorado-20261025/`: manifesto,
checkpoints de cada tentativa de raiz, resultados por link, corpos comprimidos,
resumo de progresso e CSV final. Um lock impede duas instâncias. Para uma rodada
futura, use outro diretório com `--output`; executar no mesmo diretório retoma a
rodada congelada, não reinicia sua janela ou seu orçamento de tentativas.

Resultado auditado da rodada `067db8a6afaa4a88a23ee8a15a957a1c`: encerrada em
33 minutos e 30 segundos. Foram arquivados os 2.705 resultados no PostgreSQL e
conferidos os hashes de 1.744 corpos preservados. Foram restaurados e ativados
121 links (84 do g1, 33 de A Nação, três de Rice Media e um de Nation News),
reativando um site. Outros 243 ficaram sem conclusão por espera/prazo (242 no
acesso à raiz e um na amostra textual); 2.341 ficaram sem comprovação funcional.
Todos esses não aprovados permaneceram excluídos. A auditoria confirmou modelos,
vínculos com pais ativos, intervalos preservados e eventos antes/depois de cada
restauração. Foram 3.546 requisições às raízes e 2.481 na validação textual, com
547 reutilizações de conexão registradas na fase de raízes. Não houve consulta
a robots, Bright Data ou OpenRouter nesta rodada. CSV e auditoria final estão
no mesmo diretório dos checkpoints.


## Capacidade ociosa e retentativas (26/10/2026)

Workers principais consultam primeiro a fila normal da sua etapa. Quando ela está vazia,
consomem a fila de recuperação da mesma etapa, informando a origem correta ao reservar
a tarefa. A fila de recuperação continua separada, com um worker dedicado que garante
progresso mesmo durante carga de tarefas novas. Não são criados workers adicionais.

O publicador da recuperação disponibiliza até o número configurado de workers da etapa,
limitado também pelo teto da fila e pelas mensagens já disponíveis no RabbitMQ. Em rede,
continua liberando apenas uma tarefa por host em trânsito, compartilhada com descoberta
e extração. Uma tarefa nova pronta para o mesmo host tem prioridade sobre a recuperação.
Prazos de retentativa, Retry-After, backpressure e limite de três tentativas permanecem.

Isso permite usar capacidade antes ociosa quando restam falhas de vários hosts. Não elimina
a espera quando resta um único host ou quando o servidor solicita uma pausa. Os números
do painel contam tarefas e workers; não representam uma redução automática da configuração
quando a rodada se aproxima do fim.


## Perfis de acesso por link (26/10/2026)

O campo `links.access_transport` separa conexão e mecanismo. Em uma instalação
PostgreSQL existente, aplique `sql/postgres/009_link_access_transport.sql` antes de
executar esta versão. A migração é aditiva e já foi aplicada ao ambiente atual.
Novos catálogos recebem a coluna na criação do esquema; ela deve acompanhar a
migração futura ao PowerDD. O mecanismo interpreta
RSS/sitemap e seleciona notícias. O transporte define como obter os bytes pela rede.
O padrão `direct` preserva o leitor anterior. No formulário do link, **Forma de acesso**
permite selecionar e testar outro perfil antes de salvar.

| Valor | Como acessa |
| --- | --- |
| `direct` | Cliente HTTP original com sessões públicas e conexões reutilizadas. |
| `curl_chrome_h2` | curl com perfil Chrome e HTTP/2 para HTTPS. |
| `curl_chrome_h1` | Mesmo perfil, usando HTTP/1.1. |
| `curl_firefox_h2` | curl com perfil Firefox e HTTP/2 para HTTPS. |
| `curl_bot_h2` | curl com identificação BuscadorMidiasBot e HTTP/2 para HTTPS. |
| `curl_bot_h1` | curl com identificação BuscadorMidiasBot e HTTP/1.1. |

Os perfis curl usam `curl_cffi`, não executam JavaScript e não abrem um navegador.
Em URLs HTTP sem TLS, utilizam HTTP/1.1. O protocolo efetivamente observado fica
registrado em `transport_events`, pois HTTP/2 não pode ser presumido pelo nome do perfil.
Não há troca automática de perfil em cada falha da rotina: usa-se o valor salvo no link.
A rodada de auditoria compara perfis para escolher esse valor com comprovação textual.

Todos mantêm validação TLS, fixação de IP público por conexão, checagem de escopo em
cada redirecionamento, limite de corpo, prazo, requisições, pausas e travas de host.
Resolução de IP público pode ser reutilizada por até 60 segundos dentro do processo.
Cookies curl são públicos e exclusivos da sessão; nenhum cookie autenticado é importado.
Os limites da fila continuam aplicados; não há uma camada oculta de tentativas curl.
Estes perfis não acionam Bright Data, mesmo se um formulário antigo tiver essa opção marcada.

Gerenciador e Saúde das fontes usam o perfil configurado. Novas tarefas da ingestão
conservam esse valor no snapshot do link. Tarefas antigas mantêm seu snapshot histórico,
portanto editar o cadastro não muda silenciosamente uma rodada já criada. Alterar o
perfil invalida a comprovação corrente até um novo teste compatível com a configuração.

A comparação inicial de 20 links e seus controles está em
[PILOTO_HTTP_20_LINKS.md](PILOTO_HTTP_20_LINKS.md). A recuperação ampliada, suas decisões
e limitações estão em [RECUPERACAO_TRANSPORTES.md](RECUPERACAO_TRANSPORTES.md).

Nos perfis curl, `transport_events` registra status e protocolo observados. Os
contadores de reutilização de conexão e envio de cookies do leitor original não
medem o pool interno do curl; zeros nesses campos não comprovam ausência de
keep-alive ou de cookies públicos dentro da sessão curl.
