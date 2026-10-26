# Verificação dos mecanismos antes da nova rodada — 26/10/2026

Os mecanismos e o transporte de acesso estão integrados à ingestão. A criação de rodada consulta o catálogo PostgreSQL e congela modelo, versão, parâmetros, perfil HTTP e revisões de fonte/link. O mesmo seletor de transportes e o mesmo registro de mecanismos são usados nos testes do gerenciador e no motor de ingestão. Nenhuma rodada foi iniciada nesta verificação.

## Conferências

- Os 236 RSS aprovados nas duas rodadas Feeder estão ativos, com o mesmo mecanismo e perfil HTTP dos testes; nenhuma divergência de transporte/modelo ou leitura pendente foi encontrada.
- Os seis mecanismos configurados no banco passaram pela validação de parâmetros.
- O índice diário do g1 usa `g1_daily` com `curl_chrome_h2`; os RSS do g1 mantêm seus próprios modelos e perfis validados.
- Permanecem a janela de notícias de hoje em `America/Sao_Paulo`, revalidação condicional de XML por ETag/Last-Modified, seleção temporal de filhos, profundidade máxima 10 e até três tentativas de falha por tarefa. O leitor ativo não consulta robots.txt e não usa Bright Data.
- A versão de prompt é `context-2026-10-26.1`. O código do serviço de ingestão já havia sido carregado depois da última alteração do motor, leitores e mecanismo diário do g1.
- Serviços de ingestão e painel ativos, workers com heartbeat recente nas seis etapas, seis filas principais sem mensagens e nenhum job em aberto no PostgreSQL.
- Agendamento automático permanece desligado; os horários salvos continuam 01h, 06h, 12h e 16h. O usuário pode criar uma rodada manual pelo painel.

## Ajuste realizado

A conferência identificou 33 RSS Feeder válidos que estavam classificados como `review` apenas porque a amostra textual era antiga ou não tinha data recente comprovada. Isso impediria que participassem da nova busca diária.

Essas 33 rotas foram promovidas para `updates`, após conferir cadastro ativo, XML/texto/evidência aprovados e que a revisão havia sido criada automaticamente pela própria rodada Feeder. Decisões manuais e demais rotas não foram alteradas. O histórico antes/depois foi registrado em `events` com ação `functional_rss_daily_route` e em `var/gerenciador/verificacao-mecanismos-20261026/rotas-atualizadas.json`.

Agora todos os 236 RSS aprovados participam da seleção diária. A data da notícia continua sendo filtrada na descoberta/extração; uma fonte sem publicação hoje não se transforma em erro. A regra de cadastro de novos RSS também foi corrigida para não repetir o bloqueio por idade da amostra. Essa correção não altera a classificação editorial dos artigos nem as exclusões de arquivos e outros mecanismos.

## Validação

Regressões de transporte, sessão HTTP, mecanismo diário do g1, janela diária e ingestão: 116 testes passaram; 1 teste que exige RabbitMQ de teste explícito foi ignorado. A conexão com o broker real foi conferida passivamente, sem publicar mensagens. Os testes de aplicação Feeder e propagação do transporte também passaram, incluindo a regressão de um RSS comprovado por matéria antiga.

## Espaço disponível

Na conferência operacional havia somente **420 MiB livres** no disco. O motor já adia trabalho quando a reserva cai abaixo de **128 MiB**. Os mecanismos estão preparados, mas recomenda-se liberar espaço antes de uma rodada ampla para evitar uma pausa por armazenamento. Nenhum arquivo histórico foi removido nesta verificação.
