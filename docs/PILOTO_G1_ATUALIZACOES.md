# Piloto de atualizações do g1 — 24/10/2026

O fluxo geral foi parado: serviço `buscador-midias-ingest.service` inativo e
configurações `enabled=false`, `workers_enabled=false`. As filas, rodadas e
resultados anteriores foram preservados. O painel continua disponível.

## Estratégia implementada

O mecanismo `g1_daily` lê o índice oficial
`https://g1.globo.com/sitemap/g1/sitemap.xml` e seleciona os XMLs diários declarados
que intersectam a janela, com sobreposição configurável de 0 a 2 dias (padrão 1).
Todas as páginas dos dias escolhidos são mantidas. O fuso das partições também
é configurável; o padrão é UTC. Não inventa endereços ausentes do índice.

Uma estrutura desconhecida é registrada como cobertura parcial; o mecanismo
não inicia uma varredura automática de todo o arquivo como alternativa.
Os modelos genéricos mantêm seu comportamento anterior.

Cada XML selecionado produz URLs e hashes; a comparação com o corpus ocorre em
lotes de até 100. URLs já registradas não criam novamente as tarefas custosas.
O cache guarda o índice completo interpretado, separado pelas revisões da fonte,
link e mecanismo. Cada consulta recalcula a seleção para sua janela.
Se o servidor fornecer ETag/Last-Modified, o cliente pode fazer revalidação
condicional; respostas 304 ou conteúdo idêntico dispensam nova interpretação.
O suporte desses cabeçalhos pelo g1 ainda não foi comprovado ao vivo.

Datas nos nomes dos arquivos delimitam partições, não comprovam a publicação
de cada notícia. URLs sem data explícita de publicação ainda precisam da etapa
de leitura do HTML para verificar data e texto.

## Teste com arquivos oficiais preservados

Reprodução local dos XMLs obtidos no mapeamento, com janela de 20/10/2026 em
Brasília. Não representa acesso HTTP atual, notícias de hoje ou textos coletados.

| Resultado | Primeira consulta | Segunda consulta |
|---|---:|---:|
| XMLs diários selecionados entre 9.151 | 2 | 2 |
| XMLs antigos descartados antes da leitura | 9.149 | 9.149 |
| URLs únicas registradas | 1.785 | 0 novas |
| Novas tarefas de deduplicação | 1.785 | 0 |
| Descoberta | 1,024 s | 0,760 s |
| Descoberta e comparação com corpus | 12,637 s | 0,782 s |

Os dois XMLs tinham 954 e 833 entradas; após deduplicação restaram 1.785 URLs.
Não houve chamadas OpenRouter, captura de evidências ou inserção no corpus nesse
teste. As tarefas posteriores ficaram no ambiente isolado do piloto.

Relatório completo:
`var/ingestion/g1-piloto/bm_g1_pilot_20261024_191452_05e8da/resultado.json`.

## Tentativa ao vivo

O acesso direto ao `robots.txt` retornou HTTP 503. O fallback Bright Data retornou
HTTP 407 na resposta estruturada; esse resultado é tratado como falha do proxy,
não como reprovação definitiva da fonte. A consulta de configuração confirmou
que a zona estava ativa e era Unlocker, sem esclarecer a causa do 407.

Sem obter a política de acesso, o piloto encerrou como incompleto, sem acessar
notícias nem chamar análise/evidências. Não há prova de coleta atual do g1
nesta rodada. O índice histórico não foi restaurado no catálogo de produção.

Relatório da tentativa:
`var/ingestion/g1-piloto/bm_g1_pilot_20261024_191528_b39636/resultado.json`.

## Repetição controlada

O comando exige o fluxo geral pausado e cria um schema PostgreSQL e um prefixo
Rabbit exclusivos. Consome somente suas próprias filas. Não inicia agendamento
contínuo. O modo ao vivo limita a parte custosa à amostra indicada.

```bash
scripts/bm.sh ingest g1-pilot --mode archive --day 2026-10-20 --samples 0
scripts/bm.sh ingest g1-pilot --mode live --samples 3
```

O próximo passo para completar a prova é resolver a falha de acesso e repetir
o piloto ao vivo, confirmando publicação, texto e ausência de trabalho duplicado.
