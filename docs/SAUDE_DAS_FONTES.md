# Saúde das fontes

Acesse **http://127.0.0.1:8765/saude**, também disponível no menu do painel. A rotina verifica
periodicamente se os links do catálogo ainda entregam notícia e **mantém o catálogo limpo**: o que
quebrou ou parou sai da rodada de busca, e o que voltou a funcionar volta a entrar. Ela não depende
da agenda das buscas e roda mesmo com o navegador fechado.

## Duas rotinas, em horários fixos

| Rotina | Universo | Leitura | O que pode fazer |
|---|---|---|---|
| **Links ativos** (`active`) | links ativos em sites ativos, sem exclusão lógica | **sonda leve** (`mode=probe`): lê só o documento do link, sem expandir filhos nem baixar notícias | manter, inativar, excluir ou deixar para revisão humana |
| **Inativos** (`recovery`) | links ou sites inativos | **teste completo** (`mode=full`): o mesmo `run_test` do botão Testar | reativar, só com prova |

Ambas rodam em **10h e 20h** (Brasília; `schedule_hours`), duas vezes por dia, o que dá tempo de
terminar antes da próxima rodada de busca. O padrão do DDL é `mode=full`, `scope=errors`, 24 horas,
8 sites em paralelo e 2 tentativas; o que vale é a configuração salva no banco (hoje, em `active`,
16 workers e modo `probe`; em `recovery`, 8 workers, modo `full` e `scope=inactive`).

Saúde e busca de notícias têm configurações independentes. A saúde ignora a idade da publicação
nos testes completos (`lookback_days=0`) e sempre testa pelo menos uma notícia. Não altera os
parâmetros globais dos mecanismos nem o intervalo de busca. Os limites são os do mecanismo
selecionado, congelados no início da rodada.

## Rotina de links ativos: decide e age

A sonda ([link_review.py](../src/buscador_midias/catalog/link_review.py)) lê o documento ao vivo
e propõe uma ação; [health_rules.py](../src/buscador_midias/catalog/health_rules.py) a traduz:

| Situação do link | Ação | Categoria |
|---|---|---|
| entrega notícia recente | mantém | `healthy` |
| última notícia há **mais de 1 mês** | **inativa** | `stale` |
| última notícia há **1 ano ou mais**, período do endereço já encerrado ou tema inútil | **exclui** (lógica) | `removed` |
| esporte, ou documento sem datas | nada muda; aguarda **sua revisão** | `review` |
| falha **definitiva** (404, 410, ciclo de redirecionamento) | **inativa na hora** | `access_error` |
| falha **passageira** (tempo esgotado, recusa, 5xx, documento ilegível) | **1ª vez**: nada muda; **2ª verificação seguida**: inativa | `access_error` / `invalid` |

Por que a segunda verificação: um site com um dia ruim não deve sair da rodada. A contagem de
falhas seguidas (`link_health.failures`) zera a cada verificação bem-sucedida.

**Troca de transporte antes de inativar.** Quando a falha é do tipo que outro jeito de conectar
costuma resolver (HTTP 401/402/403/406, ciclo de redirecionamento, falha TLS, conexão encerrada ou
reiniciada), a sonda repete nos perfis `curl_chrome_h1`, `curl_chrome_h2`, `curl_firefox_h2` e
`direct`. Se um passa, o `access_transport` do link é trocado (evento `health_transport`) e o
link **não** é inativado. Nunca usa `curl_bot_*`. Sem isso, um site que recusa o leitor direto mas
aceita o perfil Chrome seria tratado como quebrado.

**Hosts grandes.** Um host com mais de 100 links na rodada admite 3 testes simultâneos; os demais,
um por vez (sem isso, um site com 1.500 links serializava a cauda da rodada por mais de uma hora).

Inativação e exclusão são lógicas, auditadas em `events` (`health_inactivate`, `health_delete`) e
revisáveis. Falha de acesso **nunca** exclui sozinha.

## Rotina de inativos: reativa com prova

Um inativo só volta a ser ativo quando o teste completo comprova **notícia textual** e há
**publicação nos últimos 30 dias** (o "portão de frescor"). Um site que responde bem mas só tem
notícias antigas continua inativo; o resultado fica registrado como `healthy`, e a reativação só
acontece com a opção `activate_recovered` ligada (evento `health_reactivate`, auditado, com o
`read_order` e o transporte comprovados gravados no link).

Links excluídos, inativados manualmente sem falha comprovada, canais de áudio e falhas
exclusivamente de formato/texto não entram automaticamente na recuperação do escopo `errors`. O
escopo `inactive` considera todos os inativos visíveis; `auto_excluded_access` (só manual) retesta
as exclusões automáticas por acesso.

**A rotina agendada não testa todo o conjunto em toda rodada.** Quase toda reativação é de um link
que caiu na véspera (falha passageira que passou). Retestar toda rodada os parados há semanas só
gasta horas. Por isso, na rodada **agendada**, `recovery_due` retesta a cada rodada quem a própria
saúde inativou nos últimos 7 dias (`RECENT_DAYS`) e os demais **a cada 7 dias** (`SWEEP_DAYS`,
contados da última verificação). Exemplo: um link que caiu ontem é testado às 10h e às 20h; um que
está inativo há um mês e foi testado ontem fica de fora até a varredura semanal. A execução
**manual** ("Executar agora") sempre testa todo o escopo escolhido.

## Configuração e execução

Cada rotina permite configurar agendamento habilitado/pausado, horários fixos (ou, sem horários, a
frequência em minutos), paralelismo de 1 a 32 sites e de 1 a 3 tentativas por link. Os horários
são calculados por `next_run` no fuso de Brasília; o próximo é sempre o seguinte, nunca repõe
horários perdidos.

O agendador roda no serviço do painel (`scripts/bm.sh dashboard`). Enquanto o computador ou o
serviço estiver desligado, não há execução. Ao voltar, retoma a rodada interrompida e executa no
máximo uma rodada vencida de cada tipo. **Reiniciar o painel durante uma rodada a interrompe**
(ela é retomada); prefira reiniciar entre rodadas. Uma rodada tem uma lista finita congelada no
início; novas descobertas não aumentam essa lista.

**Executar agora** adiciona uma rodada manual à fila, mesmo com o agendamento pausado. Há no máximo
uma rodada aberta por tipo. Um lock de sessão PostgreSQL impede dois servidores de duplicarem o
trabalho. O paralelismo respeita um teste por host (três nos hosts grandes). Nenhuma transação fica
aberta durante o download. **Cancelar rodada** cancela os pendentes; o teste em andamento termina
dentro dos limites do mecanismo. Mudanças de URL/modelo/revisão durante a execução fazem o item ser
registrado como alterado, sem atualizar sua saúde.

## Resultados

| Resultado | Significado |
|---|---|
| Notícia textual confirmada (`healthy`) | Pelo menos uma amostra atende a acesso, notícia e texto |
| Parado há mais de 1 mês (`stale`) | Link ativo inativado por falta de notícia |
| Excluído (`removed`) | 1 ano ou mais sem notícia, período do endereço encerrado ou tema inútil |
| Aguarda revisão (`review`) | Esporte ou sem datas; nenhuma ação automática |
| Falha de acesso (`access_error`) | Bloqueio/permissão, HTTP ou rede impediram o acesso; repetido no limite configurado |
| Acessível, sem notícias na amostra (`no_news`) | XML válido sem notícias; não é erro de acesso |
| Sem notícia textual confirmada (`no_text`) | Conteúdo acessível, mas sem artigo textual comprovado, incluindo áudio |
| Formato ou mecanismo inválido (`invalid`) | XML/formato incompatível com o modelo |
| Inconclusivo (`inconclusive`) | A amostra não permitiu concluir, por exemplo por orçamento de leitura |
| Falha interna (`error`) | Problema do executor; não reprova a fonte |
| Cadastro alterado (`skipped`) | Item mudou depois do congelamento da rodada |

O histórico mostra rodadas, progresso, contagens por resultado, cada link testado e os resultados
de cada tentativa, com paginação e filtros (incluindo "Links ativados após recuperação"). O Painel
([Interface e API](atual/API.md)) resume as ações por dia e o balanço da última rodada de cada
rotina.

## Persistência

No esquema `midias_manager`, `health_schema.py` define cinco tabelas:

- `health_settings`: configuração e próxima execução de cada rotina (`schedule_hours`, `mode`, `scope`).
- `health_runs`: execução, configuração congelada, estado e liderança.
- `health_results`: fila finita e resultado por link, com FKs para site/link/rodada.
- `health_attempts`: resultado e categoria de cada tentativa.
- `link_health`: última avaliação aplicável, com revisões do link, site e modelo e a contagem de
  falhas seguidas (`failures`).

O índice `health_results_summary (run_id, state, category) INCLUDE (activated)` cobre as contagens
do histórico, que antes liam linhas largas (o `snapshot` em JSON) e levavam segundos. A soma de
chamadas Bright Data só é feita em rodadas que podem ter gastado (`bright_data` habilitado).

A instalação é aditiva e idempotente (`CREATE ... IF NOT EXISTS`). Alterações de configuração são
registradas em `events`. A seleção e a conferência transacional ficam em `health_store.py`; o
agendador, a retomada e o executor em `health_worker.py`; as regras de decisão em
`health_rules.py`; as ações e a trilha de auditoria em `health_actions.py` e
`health_activation.py`; a classificação do teste em `health_policy.py`. Essas tabelas e regras devem
acompanhar a futura transferência da aplicação ao powerDD.

## Bright Data

O fallback está integrado ao teste individual e às duas rotinas. A opção é configurável por
rotina, com contagem de chamadas e limite por teste, e hoje está desligada (as credenciais ainda
precisam ser configuradas). A reativação de recuperados exige a mesma prova de notícia textual do
teste direto. Ver [Bright Data nos testes e na saúde](BRIGHT_DATA_TESTES.md).

## Verificação

Testes de regras e integrações com PostgreSQL descartável cobrem a decisão por resultado da sonda,
a histerese, os horários fixos, a troca de transporte, os hosts grandes, o portão de frescor, a
seleção semanal de inativos, a seleção das duas filas, limites de tentativas, retomada,
cancelamento, revisão de cadastros, configuração, autorização HTTP e liderança entre dois workers.
A rede é simulada. A interface é conferida em navegador nas larguras de desktop e celular.
