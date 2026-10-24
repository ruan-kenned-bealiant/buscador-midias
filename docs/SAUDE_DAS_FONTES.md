# Saúde das fontes

Acesse **http://127.0.0.1:8765/saude**, também disponível no menu do gerenciador.
A verificação usa o mesmo `run_test` dos testes individuais: lê RSS/sitemap com o
modelo atribuído ao link, expande os filhos nos limites do mecanismo e testa uma
amostra de notícias quanto a acesso, classificação e texto.

## Duas rotinas

- **Links ativos:** todos os links ativos em sites ativos, sem exclusão lógica.
- **Inativos com falhas de acesso:** links ou sites inativos cuja última evidência
  aplicável à configuração atual aponta falha de acesso, rede ou permissão.
  Considera o último teste manual ou a última verificação de saúde compatível.
  Alterar somente estado, observações ou intervalo não invalida a evidência de acesso.

Links excluídos, inativados manualmente sem falha comprovada, canais de áudio e
falhas exclusivamente de formato/texto não entram automaticamente na recuperação.
Se um inativo volta a entregar notícia textual, o histórico registra a recuperação,
o cadastro permanece inativo quando a opção de ativar recuperados estiver desligada.
Com a opção ligada, a nova prova textual permite ativar o link de forma auditada,
preservando sites inativados manualmente.

Saúde e busca de notícias têm configurações independentes. A saúde ignora a idade
da publicação (`lookback_days=0`) e sempre testa pelo menos uma notícia. Os demais
limites são os do mecanismo selecionado, congelados no início da rodada. Não altera
os parâmetros globais ou o intervalo de busca. As provas anteriores são preservadas
no histórico; a ativação dos recuperados é opcional e auditada.

## Configuração e execução

Cada rotina permite configurar agendamento habilitado/pausado, frequência em
minutos, paralelismo de 1 a 32 sites e de 1 a 3 tentativas por link. O padrão inicial
é **24 horas**, **8 sites em paralelo**, **2 tentativas**, com a primeira execução
programada para 24 horas depois da instalação. Não existe teto de quatro horas
para a frequência dessas rotinas ou para a configuração futura das buscas.

O agendador roda no serviço do painel, sem precisar manter o navegador aberto.
Enquanto o computador ou o serviço estiver desligado, não há execução. Ao voltar,
retoma a rodada interrompida e executa no máximo uma rodada vencida de cada tipo;
não cria uma fila de todas as repetições perdidas. Uma rodada tem uma lista finita
congelada no início; novas descobertas não aumentam essa lista.

**Executar agora** adiciona uma rodada manual à fila, mesmo com o agendamento
pausado. Há no máximo uma rodada aberta por tipo e uma rodada sendo processada por
esquema. Um lock de sessão PostgreSQL impede dois servidores de duplicarem o
trabalho. O paralelismo respeita um teste por host. Nenhuma transação fica aberta
durante o download. O próximo agendamento não se sobrepõe à rodada anterior.

**Cancelar rodada** cancela os pendentes; o teste em andamento termina dentro dos
limites do mecanismo. As tentativas já concluídas continuam no histórico.
Mudanças de URL/modelo/revisão ou do cadastro durante a execução fazem o item ser
registrado como alterado, preservando o teste no histórico sem atualizar sua saúde.

## Resultados

| Resultado | Significado |
|---|---|
| Notícia textual confirmada | Pelo menos uma amostra atende a acesso, notícia e texto |
| Falha de acesso | Bloqueio/permissão, HTTP ou rede impediram o acesso; pode ser repetido no limite configurado |
| Acessível, sem notícias na amostra | XML válido sem notícias; não é erro de acesso |
| Sem notícia textual confirmada | Conteúdo acessível, mas sem artigo textual comprovado, incluindo áudio |
| Formato ou mecanismo inválido | XML/formato incompatível com o modelo |
| Inconclusivo | A amostra não permitiu concluir, por exemplo por orçamento de leitura |
| Falha interna | Problema do executor; não reprova a fonte |
| Cadastro alterado | Item mudou depois do congelamento da rodada |

O histórico mostra rodadas, progresso, contagens por resultado, cada link testado e
os resultados de cada tentativa. É possível paginar e filtrar os links. A confirmação
é por amostra, tal como no teste individual; não exige varrer todas as notícias.

## Persistência e migração futura ao powerDD

No esquema `midias_manager`, `health_schema.py` define cinco tabelas:

- `health_settings`: configuração e próxima execução de cada rotina.
- `health_runs`: execução, configuração congelada, estado e liderança.
- `health_results`: fila finita e resultado por link, com FKs para site/link/rodada.
- `health_attempts`: resultado e categoria de cada tentativa.
- `link_health`: última avaliação aplicável, com revisões do link, site e modelo.

A instalação é aditiva e idempotente. Não reimporta o SQLite, não modifica históricos
da rodada 12 e não apaga resultados anteriores. Alterações de configuração são
registradas em `events`. A seleção/conferência transacional fica em `health_store.py`;
o agendador, retomada e executor em `health_worker.py`; a política em `health_policy.py`.
Essas tabelas e regras devem acompanhar a futura transferência da aplicação ao powerDD.

## Bright Data

O fallback está integrado ao teste individual e às duas rotinas de saúde. A opção
é configurável por rotina, com contagem de chamadas e limite por teste. A ativação
opcional de recuperados exige a mesma prova de notícia textual do teste direto.
Ver [Bright Data nos testes e na saúde](BRIGHT_DATA_TESTES.md).

## Verificação

Testes de política e integrações com PostgreSQL descartável cobrem seleção das duas
filas, texto antigo, limites de tentativas, retomada, cancelamento, revisão de cadastros,
configuração, autorização HTTP e liderança entre dois workers. A rede é simulada.
A interface é conferida em navegador nas larguras de desktop e celular.
