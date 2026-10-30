# Saúde das fontes

A saúde é uma rodada como as outras (`bm_runs` com `type='health'`), conduzida pelo serviço
`buscador-midias-health`. Só um líder por banco trabalha (advisory lock).

| Rotina | Escopo |
|---|---|
| `active` | Links ativos de sites ativos. |
| `recovery` | Links inativos cuja última verificação foi falha de acesso (ou todos, manual). |

Horários padrão (Brasília): a `active` às 10h e 20h; a `recovery` às 09h. Cada rodada congela os
elegíveis e a revisão de cada link.

**Uma rodada de cada vez.** A saúde e a busca não rodam juntas. Uma rodada de saúde agendada não
começa enquanto houver uma busca aberta (continua devida e sai quando a busca fecha), e a manual é
recusada com 409. A busca tem hora marcada (tolerância de 15 min) e por isso não espera: se uma saúde
estiver em andamento, ela **cede a vez**, deixa de reservar links novos, termina os que já estão em
teste e retoma quando a busca fecha. As rodadas de saúde entre si já são em sequência.

**Ritmo por site.** A rodada lê um link por vez de cada site pequeno e espera 3 s entre duas
leituras do mesmo site (sem isso, sites pequenos respondem 429 e o link parecia quebrado). Depois de um
429 o site descansa 30 s (ou o `Retry-After` que ele pediu, no máximo 300 s), inclusive os sites
grandes (100 links ou mais), que normalmente leem 3 por vez.

## Modos

- **`probe`** (ativos): uma leitura leve do documento. Decide `keep`, `inactivate`, `delete` ou
  `review` pelas regras de frescor, período encerrado e tema inútil. Recusa (401, 403, 406) e
  documento ilegível inativam na segunda verificação seguida; 404, 410 e ciclo de redirecionamento
  inativam na hora. Falha que passa sozinha (429, 5xx, timeout) só inativa depois de 6 verificações
  seguidas (cerca de 3 dias): o site pode ter tido um dia ruim, ou nós o sobrecarregamos.
  Se o transporte do link foi recusado, tenta os outros e, achando um que funcione, troca o
  transporte (a prova antiga é descartada).
- **`full`** (teste completo): o mesmo teste do Gerenciador, em amostra, sem alterar o cadastro,
  salvo a reativação opcional.

## Reativação e restauração

Com `activate_recovered`, um link inativo volta a ficar ativo se o teste completo comprova texto de
notícia **e** o portão de frescor libera. O escopo `auto_excluded_access` restaura o que a validação
excluiu por falha de acesso, junto com o site-pai quando a validação também o inativou. Decisões do
usuário (`deactivated_by='user'`) nunca são desfeitas.

## Onde fica o resultado

`bm_run_links` (um por link, com `attempt_log`), `bm_link_state.health_*` (a última verificação e o
que ela provou) e `bm_events` (cada inativação, exclusão, troca de transporte e reativação).
