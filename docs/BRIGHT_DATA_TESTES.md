# Bright Data nos testes e na saúde

O teste do gerenciador e as rotinas de Saúde das fontes usam o mesmo motor.
Quando habilitado, o transporte tenta **HTTP direto primeiro** e recorre ao Web
Unlocker para bloqueios HTTP 403/429, desafios antibot reconhecidos e determinadas
falhas de rede. Não usa o serviço para 401 de autenticação, 404, XML vazio, áudio
ou ausência de texto. A recuperação de robots.txt também pode usar fallback;
uma política obtida que proíba o caminho continua sendo respeitada.

## Gerenciador

Em Editar/Testar link, a opção **Usar Bright Data como fallback** fica disponível
quando `BRIGHTDATA_API_KEY` e `BRIGHTDATA_ZONE_UNLOCKER` estão no servidor. A zona e
a chave não são enviadas ao navegador. O padrão do teste individual é habilitar o
fallback quando há credenciais, podendo desmarcá-lo para testar somente acesso direto.
O teste individual ignora a idade das notícias para comprovar funcionamento e usa
até 60 segundos quando o fallback está ligado. Os parâmetros globais do mecanismo
para buscas futuras são preservados. O resultado mostra as opções efetivamente
usadas, o número de chamadas enviadas e os endereços/status envolvidos.
Esse contador não representa valor faturado nem uma estimativa em dólares.

## Saúde das fontes

Cada rotina permite habilitar o Bright Data e configurar o máximo de chamadas ao
provedor **por teste** (1–48; padrão 8), além dos limites de requisições do mecanismo.
A saúde reserva até 60 segundos por teste quando usa o fallback. As configurações
são congeladas no início de cada rodada. A revisão e a política das buscas de
notícias não são alteradas. A contagem da rodada soma todas as tentativas.

Na recuperação, **Ativar os links recuperados** habilita ativação com prova textual.
**Escopo da execução manual** permite testar apenas os inativos com falha de acesso
ou todos os inativos visíveis, incluindo aqueles que já tiveram comprovação anterior.
O agendamento periódico continua selecionando os inativos com falhas de acesso.

A ativação exige RSS/sitemap compatível, pelo menos uma notícia textual e cadastro
sem mudanças durante o teste. Mantém site/mecanismo, intervalo e observações,
salva a nova prova em `last_test`/`link_test_results` e o evento `health_reactivate`.
Sites inativados manualmente e exclusões manuais permanecem preservados.
Exclusões automáticas por falha de acesso exigem o escopo manual descrito abaixo. O histórico mostra o selo de link ativado
e permite filtrar os ativados.

## Transporte e falhas

`catalog/bright_reader.py` estende o leitor existente, preservando escopo, DNS público,
política de robots, limites de bytes, tempo, requisições e redirecionamentos. O cliente
`net/brightdata.py` usa o envelope `format=json` nesses testes para distinguir o
status HTTP da API do status do site. Os consumidores antigos continuam usando
`format=raw`, com o comportamento anterior.

Respostas do provedor com erro de autenticação, configuração ou transporte ficam
registradas como falha do fallback/inconclusivo e não justificam ativar ou reprovar
a fonte. O limite de chamadas do fallback também sinaliza resultado parcial. Uma
resposta HTTP 200 por si só nunca confirma funcionamento: a extração textual segue
os mesmos critérios do teste direto.

Os resultados guardam `bright_data_enabled`, `bright_data_requests`, eventos de
fallback, sinalização de falha/limite do provedor e `access_method`. A evidência do
link indica quando Bright Data foi usado. URLs e critérios continuam os mesmos;
o fallback não descobre novos caminhos nem expande a fila da rodada.

Definição da API: https://docs.brightdata.com/api-reference/rest-api/unlocker/unlock-website

## Recuperar exclusões automáticas por acesso

O escopo manual **Excluídos automaticamente por falha de acesso** inclui somente
links cuja última alteração auditada foi `validation_exclude`, com revisão atual
igual à decisão e teste anterior classificado como erro de acesso. Exclui áudio,
ausência de texto, exclusões manuais, pais excluídos e sites inativados manualmente.
A fila é congelada; nenhuma descoberta adiciona itens. Usa até duas tentativas
conforme a configuração da rotina, sem filtro de idade da notícia.

Somente um teste com notícia textual comprovada restaura `deleted_at` e ativa o
link. Se o site foi inativado pela validação por ficar vazio, a mesma comprovação
permite reativá-lo, com evento `health_restore_site` associado à rodada. Os outros
links do site reconhecem essa alteração feita pela própria rodada; qualquer edição
externa de site, link ou mecanismo invalida o teste pendente. Os eventos originais,
as exclusões e os resultados históricos permanecem no banco. O selo **Ativado**
e o filtro de ativados também identificam estes links restaurados.
