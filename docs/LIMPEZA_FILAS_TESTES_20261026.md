# Limpeza de filas de pilotos e cache de testes — 26/10/2026

O broker próprio tinha 90 filas. Foram removidas **59 filas vazias de pilotos de
24/10**, usando nomes restritos às execuções identificadas e as condições
`--if-empty --if-unused` em cada remoção. Nenhuma mensagem foi descartada.

Restaram 31 filas:

- 18 `midias.daily.v1.*`: fluxo diário atual, preservadas integralmente.
- 12 `midias.ingest.*`: fluxo anterior com mensagens, preservadas.
- 1 fila `midias.sources_pilot.*.fetch.failed`: contém três mensagens históricas,
  preservada por não estar vazia.

Os inventários e ações estão em
`var/maintenance/queue-cleanup-20261026/{before,actions,after,summary}.json`.
Os inventários são observações de instantes diferentes; contagens das filas ativas
podem mudar durante a execução. A verificação confirmou que todas as filas removidas
estavam vazias no inventário e que as 18 filas atuais e 12 antigas permaneceram.

Foram removidos 138 arquivos gerados em `tests/**/__pycache__`, liberando
4.382.747 bytes. Os 137 arquivos Python da suíte foram examinados estruturalmente:
não foram encontrados arquivos completos com AST idêntica. Isso não prova ausência
de casos sobrepostos, mas não sustenta remover testes em massa por duplicação.

Os testes de regressão continuam preservados. Alguns cobrem ferramentas de pilotos
ou subsistemas antigos que ainda existem no repositório. Sua retirada deve acompanhar
a desativação explícita desses comandos e rotas; parsers e regras reutilizados pelo
produto atual devem manter cobertura. Relatórios e evidências históricos não foram
apagados. Não houve alteração do runtime, reinício de serviços ou purge de filas.
