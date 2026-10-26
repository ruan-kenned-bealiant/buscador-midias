# Piloto de transportes HTTP — 20 links problemáticos

Executado em 26/10/2026. Rodada encerrada, com **20 URLs previamente problemáticas de seis veículos**: g1 (4), Reuters (4), Bloomberg (3), NYT (3), Band (3) e AP (3). São **12 endpoints XML, uma página de catálogo e sete artigos**, totalizando 20 URLs.

## Conclusão

**O transporte faz diferença, mas não existe uma solução universal.** O curl recuperou os quatro caminhos do g1, incluindo XML e notícia textual do dia. A AP entregou uma notícia textual através de seu sitemap de notícias, com sucesso também nas duas repetições posteriores. O NYT abriu um artigo uma vez, mas as duas repetições posteriores falharam: não foi considerado resolvido. Reuters e Bloomberg permaneceram bloqueados nas variantes testadas. Os três endereços da Band retornaram 404 com curl, indicando necessidade de rever os endereços, além do transporte.

Nenhum cadastro foi ativado, desativado ou alterado por este piloto. Nenhuma rodada de ingestão foi iniciada, nenhum transporte de produção foi trocado, e não houve chamadas à Bright Data ou OpenRouter. Os arquivos do mapeamento anterior foram preservados.

## Comparação principal

Foram cinco avaliações nos mesmos 20 links: HTTP atual; curl com perfil Chrome; curl com sessão iniciada pela home; Chromium; e repetição do HTTP atual após as outras variantes do veículo. Cada variante recebeu contexto/sessão próprios. O HTTP atual não compartilhou cookies do curl ou do browser.

| Variante | Links com acesso 2xx / 20 | XML válido / 12 | Caminho até notícia textual / 20 | Mediana de duração |
|---|---:|---:|---:|---:|
| HTTP atual | 0 | 0 | 0 | 0.182 s |
| curl · perfil Chrome | 7 | 6 | 6 | 0.338 s |
| curl · home primeiro | 6 | 6 | 5 | 0.366 s |
| Chromium | 4 | 4 | 4 | 4.604 s |
| HTTP atual repetido | 0 | 0 | 0 | 0.161 s |

A mediana acima inclui falhas rápidas e **não** representa velocidade de coleta bem-sucedida. O HTTP atual foi rápido porque recusou todos os alvos. Um sucesso exige abrir o endpoint e confirmar artigo e texto; XML 200 sem artigo não conta como caminho textual. A página HTML do catálogo Reuters não é promovida a feed nem a notícia.

As 100 avaliações principais levaram **88.7 segundos de tempo decorrido**, com três veículos em paralelo e chamadas seriadas por veículo. Houve mais 18 controles e cinco repetições finais, totalizando **123 avaliações**. Cada avaliação pode incluir vários downloads; esse número não é uma contagem de chamadas HTTP nem uma estimativa para uma rodada completa de ingestão.

## Resultado por veículo

- **g1:** os quatro endpoints retornaram 503 no HTTP atual e 200 com curl e Chromium. Foram abertos o índice central, XML do dia e artigo, além dos RSS geral, política e Acre. Exemplo do índice: filho `2026/10/26_9.xml`, notícia sobre a votação em Presidente Prudente, publicada em 26/10/2026, com 1.310 caracteres úteis extraídos. Isso comprova caminhos amostrados, não cobertura integral do g1.
- **AP:** `https://apnews.com/news-sitemap.xml` → `news-sitemap-content.xml` → reportagem textual sobre os playoffs da MLB, publicada em 26/10/2026, com 2.321 caracteres. Três avaliações com perfil Chrome (inicial e duas repetições) confirmaram o mesmo artigo. É uma amostra esportiva; não comprova todas as editorias. `index.rss` retornou 401 com curl. O sitemap geral abriu, mas o caminho amostrado levou a uma página de autor, corretamente recusada como notícia.
- **NYT:** um dos três artigos retornou 200 e 2.434 caracteres com curl direto na primeira passagem. Home primeiro, Chromium, curl com identificação do bot e as duas repetições do curl direto falharam nesse artigo. Resultado intermitente; não marcar o domínio como recuperado.
- **Reuters:** os dois índices, a página HTML do sitemap e o artigo testado continuaram em 401. O artigo é histórico; a avaliação de transporte não aplicou filtro de publicação.
- **Bloomberg:** os três artigos continuaram em 403, inclusive no Chromium limpo.
- **Band:** os três candidatos retornaram 403 no cliente atual/Chromium e 404 com os perfis Chrome do curl. Não se pode ativar esses candidatos com base apenas na mudança de status.

## Controles para interpretar a causa

Em um alvo de cada veículo, fizemos três controles: HTTP atual mudando apenas o User-Agent para Chrome; curl com perfil Chrome mantendo `BuscadorMidiasBot/0.1`; e curl comum sem perfil Chrome mantendo o identificador do bot.

- Trocar **somente o User-Agent** não recuperou nenhum dos seis controles.
- No índice central do **g1**, curl funcionou também sem perfil de navegador, mantendo o identificador do bot. Um controle adicional forçando **HTTP/1.1** continuou entregando XML e notícia textual.
- Portanto, para o caso g1 testado, **JavaScript, troca do identificador e HTTP/2 não foram requisitos necessários**. Ainda não isolamos qual diferença entre os clientes (TLS, cabeçalhos, compressão, conexão ou atendimento pelo edge) explica a resposta. Não atribuímos a causa exclusivamente à assinatura TLS.
- A sessão iniciada pela home não trouxe recuperação exclusiva. No artigo NYT que abriu com curl direto, essa variante falhou. Não há evidência para adicioná-la indiscriminadamente ao fluxo.
- Chromium não recuperou nenhum caminho que curl não tivesse recuperado; foi mais caro em tempo nos mesmos quatro alvos g1. O teste não sustenta usar um navegador em todos os workers.

## Proposta para uma implementação posterior

1. Criar uma opção de transporte curl compartilhada pelo teste de mecanismos, saúde e ingestão, com TLS verificado, proteção de destinos públicos, timeout, limite de resposta e orçamento de tentativas.
2. Começar por g1 e pela rota de notícias da AP. No g1, priorizar o perfil curl comum mantendo a identificação do bot, antes de exigir emulação de navegador.
3. Registrar por domínio o perfil efetivamente comprovado, preservando no log o transporte usado para XML, artigo e evidência. O mesmo mecanismo de parsing/seleção continua sendo utilizado.
4. Manter Chromium como ferramenta pontual de diagnóstico até haver casos de ganho exclusivo; não como fallback obrigatório para todo 403/503.
5. Não ativar NYT, Reuters, Bloomberg ou os candidatos Band apenas por causa deste piloto. Para Band, remapear as URLs; para recusas persistentes, buscar outra distribuição oficial ou acesso autorizado.

## Limites e reprodução

- Chromium headless 148.0.7778.96, com contexto limpo, sem login/perfil pessoal. JavaScript habilitado; imagens, mídia e fontes bloqueadas para limitar tráfego. Até 120 requisições de recursos e até quatro segundos de espera para HTML. Esse ambiente não é idêntico ao Chrome pessoal do usuário.
- Não foram usados resolução de CAPTCHA, alteração de conteúdo recebido, plugins de ocultação ou remoção de paywall. Texto útil não significa necessariamente artigo integral.
- curl_cffi 0.17.0 e Playwright 1.64.1, instalados como ferramentas opcionais. O Chrome já estava disponível; não foi baixado outro navegador.
- Até 45 segundos por avaliação, seis leituras lógicas (incluindo home/XMLs/artigo), dois níveis de expansão XML e uma amostra de notícia. Teto global da passagem principal: 20 minutos. Falhas sem notícia em uma amostra não provam ausência de outros caminhos.
- Todos os transportes verificaram TLS. O cliente original e curl fixaram IP público nas conexões; o Chromium validou destinos DNS públicos no roteamento. O código do piloto não é uma implementação pronta para produção.
- Nenhuma resposta trouxe `Retry-After`. O HTTP original foi repetido depois das outras variantes do veículo, em baixa concorrência, mas isso não substitui uma investigação de disponibilidade ao longo de horas.
- Corpos recebidos e DOM renderizado foram preservados separadamente; SHA-256, status, datas, versões e capturas visuais constam nos artefatos. Cookies ficaram somente em memória; os relatórios guardam apenas a quantidade.

Ferramentas usadas: [curl_cffi](https://curl-cffi.readthedocs.io/en/latest/quick_start.html) e [rede do Playwright](https://playwright.dev/python/docs/network).

Instalação reproduzível: `.venv/bin/python -m pip install ".[access-pilot]"`. Execução principal: `PYTHONPATH=src .venv/bin/python scripts/pilot_http_transports.py`. O script usa o Chrome em `/opt/google/chrome/chrome`, os alvos congelados e checkpoints desta rodada. Executá-lo novamente preserva as medições existentes; outra rodada deve usar outro diretório.

## Matriz dos 20 links

“Texto” significa notícia com texto útil confirmada no caminho amostrado. “XML, sem texto” significa somente acesso/estrutura, sem notícia textual comprovada. A planilha contém as URLs completas.

| ID | Veículo | HTTP atual | curl Chrome | curl + home | Chromium | HTTP repetido |
|---:|---|---|---|---|---|---|
| 1 | g1 | 503 | 200 · texto | 200 · texto | 200 · texto | 503 |
| 2 | g1 | 503 | 200 · texto | 200 · texto | 200 · texto | 503 |
| 3 | g1 | 503 | 200 · texto | 200 · texto | 200 · texto | 503 |
| 4 | g1 | 503 | 200 · texto | 200 · texto | 200 · texto | 503 |
| 5 | Reuters | 401 | 401 | 401 | 401 | 401 |
| 6 | Reuters | 401 | 401 | 401 | 401 | 401 |
| 7 | Reuters | 401 | 401 | 401 | 401 | 401 |
| 8 | Reuters | 401 | 401 | 401 | 401 | 401 |
| 9 | Bloomberg News | 403 | 403 | 403 | 403 | 403 |
| 10 | Bloomberg News | 403 | 403 | 403 | 403 | 403 |
| 11 | Bloomberg News | 403 | 403 | 403 | 403 | 403 |
| 12 | The New York Times | 403 | 200 · texto | 403 | 403 | 403 |
| 13 | The New York Times | 403 | 403 | 403 | 403 | 403 |
| 14 | The New York Times | 403 | 403 | 403 | 403 | 403 |
| 15 | Band Jornalismo | 403 | 404 | 404 | 403 | 403 |
| 16 | Band Jornalismo | 403 | 404 | 404 | 403 | 403 |
| 17 | Band Jornalismo | 403 | 404 | 404 | 403 | 403 |
| 18 | Associated Press | 403 | 401 | 401 | 403 | 403 |
| 19 | Associated Press | 403 | 200 · texto | 200 · texto | 403 | 403 |
| 20 | Associated Press | 403 | 200 · XML, sem texto | 200 · XML, sem texto | 403 | 403 |

## Arquivos de conferência

- [Comparativo com as 20 URLs](../var/gerenciador/piloto-http-20-20261026/comparativo.csv)
- [Todas as medições, inclusive controles](../var/gerenciador/piloto-http-20-20261026/medicoes.csv)
- [Resumo e propostas por domínio](../var/gerenciador/piloto-http-20-20261026/resumo.json)
- [Alvos e orçamento congelados](../var/gerenciador/piloto-http-20-20261026/alvos.json)
- `medicoes/*.json`: etapas, exemplos, tempos, erros e quantidade de cookies, sem valores de sessão.
- `corpos/<id>/<variante>/`: respostas `.body.gz`, respostas recebidas pelo browser `.received.gz` e capturas `.png`.
- `controles-execucao.py`, `repeticoes-execucao.py` e `repeticoes-ap-execucao.py`: execução dos controles, sem credenciais.

Validação do código de aferição: 48 testes passaram, incluindo rejeição de desafio HTML com HTTP 200, separação entre XML e notícia e distinção de podcast. Lint passou e `pip check` não apontou dependências quebradas.
