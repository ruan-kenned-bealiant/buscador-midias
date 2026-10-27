# Documentação do buscador de mídias

Referência da implementação revisada em **26/10/2026**. Descreve o código do repositório, não uma garantia de disponibilidade dos sites externos nem um retrato permanente da rodada em execução.

O produto mantém um catálogo de sites e de seus RSS/sitemaps, testa esses caminhos e alimenta um corpus de notícias com análise de contexto e evidência HTML. O gerenciador e o buscador usam PostgreSQL; a ingestão distribui trabalho em seis etapas pelo RabbitMQ.

## Por onde começar

| Quero entender… | Documento |
|---|---|
| Componentes, responsabilidades e limites do produto | [Arquitetura](ARQUITETURA.md) |
| Da leitura do RSS até a gravação de uma notícia | [Fluxo de notícias](FLUXO_NOTICIAS.md) |
| Cadastro, mecanismos, transportes HTTP e saúde | [Catálogo e mecanismos](CATALOGO_MECANISMOS.md) |
| Tabelas, relacionamentos e arquivos persistidos | [Dicionário de dados](DADOS.md) |
| Telas, endpoints, formatos e controles de edição | [Interface e API](API.md) |
| Ambiente, inicialização, diagnóstico e manutenção | [Operação e desenvolvimento](OPERACAO.md) |
| Localização dos módulos, scripts e testes | [Inventário do repositório](INVENTARIO.md) |
| Contexto individual, lotes de IA e gravação por entidade | [Contexto por entidade](../CONTEXTO_POR_ENTIDADE.md) |

Para o usuário do painel, o guia didático está em `/guia`. Para manutenção de código, comece pela arquitetura e pelo inventário; para operação, leia os pré-requisitos antes de executar comandos.

## Como interpretar esta documentação

- **Mecanismo** interpreta e seleciona documentos; **transporte** faz a conexão HTTP. São escolhas diferentes do link.
- **Teste funcional** confirma uma amostra de notícia textual. Não comprova cobertura integral do site ou disponibilidade futura.
- **Rodada** congela links, configurações e janela. **Tarefa** é uma execução de uma etapa. Uma notícia pode aparecer em vários links e ter processamento compartilhado.
- **Corpus** é o destino de notícias; **catálogo** é o cadastro de caminhos; **histórico de ingestão** registra o processamento. Não são a mesma tabela.
- Valores indicados como padrões vêm do código. A configuração salva no PostgreSQL pode ser diferente.

## Auditoria de cobertura preparada

A [comparação das capas de 120 fontes](../AUDITORIA_COBERTURA_CAPAS.md) aguarda o
encerramento da rodada congelada. O documento explica alvos, denominadores,
limitações e os artefatos de resultado.

## Histórico e documentos especializados

Os arquivos anteriores continuam disponíveis para rastreabilidade. Contagens, horários e conclusões de auditorias datadas valem para a execução citada, e não representam automaticamente o estado atual da base.

- [Plano inicial do buscador](../PLANO_BUSCADOR_NOTICIAS.md), [implementação do buscador](../BUSCADOR_NOTICIAS.md) e [fluxo diário](../FLUXO_DIARIO.md).
- [Migração PostgreSQL](../MIGRACAO_POSTGRES_GERENCIADOR.md), [modelo relacional](../MODELO_RELACIONAL_CATALOGO.md), [saúde](../SAUDE_DAS_FONTES.md), [acesso HTTP](../HTTP_ACESSO.md) e [classificação](../CLASSIFICACAO_ARTIGOS.md).
- [Validação do catálogo](../VALIDACAO_COMPLETA_CATALOGO.md), [recuperação de transportes](../RECUPERACAO_TRANSPORTES.md), [mapeamento editorial](../MAPEAMENTO_EDITORIAL_PRIORITARIO.md) e [Feeder](../MAPEAMENTO_FEEDER.md).
- [Limpeza de filas de pilotos e cache de testes](../LIMPEZA_FILAS_TESTES_20261026.md).
- [Verificação dos mecanismos](../VERIFICACAO_MECANISMOS_20261026.md) e [compactação de evidências](../LIMPEZA_ESPACO_20261026.md).
- O [README original](../../README.md) e o [DESIGN](../../DESIGN.md) também conservam o histórico do mapeamento e da CLI anterior. Para o comportamento operacional atual, use esta seção e confira os arquivos de implementação vinculados.

A revisão percorreu os módulos Python, os recursos HTML/JavaScript, os scripts, as definições SQL, a implantação e os testes. Não executou nova coleta, não consumiu provedores e não reiniciou os serviços da rodada em andamento. As verificações desta entrega são documentais; resultados de testes citados nos relatórios anteriores pertencem às respectivas entregas.

## Otimização aplicada à ingestão

Consulte [mudanças, medições e limites](../PLANO_OTIMIZACAO_DESCOBERTA_CORPUS.md) para
processamento em lote, pools de conexão, reutilização de XML e desacoplamento das filas.
