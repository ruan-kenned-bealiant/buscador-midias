"""Guia de interpretação com mecanismos e parâmetros obtidos do registro real."""

from html import escape
from importlib import resources

from buscador_midias.catalog.mechanisms import MECHANISMS

# Significados de produto, independentes das contagens de qualquer rodada.
RESULTS = (
    (
        "healthy",
        "Notícia textual confirmada",
        "O XML é compatível e pelo menos uma página da amostra entregou texto útil de notícia.",
        "Você testa um RSS com dez entradas. O sistema abre até três páginas e consegue "
        "ler o corpo de uma reportagem. Acesso, notícia e texto foram comprovados nessa amostra.",
        "É prova de funcionamento na data do teste, sem garantir todas as notícias ou "
        "disponibilidade futura.",
    ),
    (
        "invalid",
        "Formato ou mecanismo inválido",
        "A resposta não pôde ser interpretada como XML aceito, ou não corresponde ao "
        "mecanismo escolhido.",
        "Você cadastrou a página inicial do jornal como RSS. Ela abre no navegador, mas "
        "entrega uma página comum (HTML), não um boletim RSS. Outro exemplo: escolher "
        "Sitemap direto para um índice que lista outros arquivos.",
        "Confira a URL e o formato. Troque o mecanismo quando houver incompatibilidade"
        " e teste novamente.",
    ),
    (
        "no_news",
        "Acessível, sem notícias na amostra",
        "O documento pôde ser lido, mas a varredura não encontrou URLs de notícias aproveitáveis.",
        "O boletim RSS abre e seu formato está correto, mas não contém entradas. O sistema "
        "não recebe um endereço de reportagem para testar. A lista foi acessada; a "
        "reportagem ainda não pôde ser avaliada.",
        "Não é erro de acesso nem prova de que o site inteiro não tenha notícias. "
        "Confira as entradas e os avisos.",
    ),
    (
        "no_text",
        "Sem notícia textual confirmada",
        "Páginas da amostra foram acessadas, mas nenhuma confirmou texto útil de notícia.",
        "Podcast, vídeo, página de categoria, paywall ou texto carregado por "
        "JavaScript que o extrator não conseguiu ler.",
        "Abra as amostras e confira os motivos. Texto visível no navegador pode exigir"
        " melhoria na extração.",
    ),
    (
        "access_error",
        "Falha de acesso",
        "Não foi possível acessar o RSS/sitemap ou uma notícia necessária à comprovação.",
        "HTTP 403/429, endereço inexistente (404), timeout, DNS, SSL ou falha no destino.",
        "Confira se a falha ocorreu na lista XML ou na reportagem. Leia o código e a "
        "mensagem antes de alterar o mecanismo: trocar o leitor não resolve "
        "necessariamente um bloqueio de rede. Veja os exemplos em Problemas de acesso.",
    ),
    (
        "stale",
        "Parado há mais de 1 mês (inativado)",
        "O link estava ativo, mas o próprio feed ou sitemap não traz notícia nova há mais "
        "de um mês. A rotina de saúde o inativou.",
        "Um RSS que parou de publicar há 45 dias.",
        "O link sai das buscas, sem ser apagado. A rotina de inativos o reativa se ele "
        "voltar a publicar.",
    ),
    (
        "removed",
        "Excluído (1 ano, período encerrado ou tema inútil)",
        "A rotina de saúde excluiu o link: sem notícia há 1 ano ou mais, endereço de um "
        "período que já fechou (por exemplo o sitemap de um dia ou mês passado) ou "
        "tema que não interessa (clima, horóscopo, receita).",
        "/2026/10/19_1.xml, o sitemap de um único dia que já passou.",
        "A exclusão é lógica e fica na auditoria; pode ser revista. Falha de acesso nunca "
        "exclui sozinha.",
    ),
    (
        "review",
        "Aguarda sua revisão (esporte ou sem data)",
        "A rotina não consegue decidir sozinha: o link é de esporte, ou o documento não "
        "traz datas que provem a atualidade.",
        "Um feed de resultados de campeonato.",
        "Nada muda no link. Decida se ele fica ou sai.",
    ),
    (
        "inconclusive",
        "Inconclusivo",
        "Não há evidência suficiente para confirmar funcionamento nem atribuir uma "
        "reprovação específica.",
        "O índice abriu, mas tem muitos arquivos filhos. O tempo do teste acabou antes "
        "de chegar a uma reportagem. Sem essa amostra, ainda não há prova de texto. "
        "Limites do fallback ou falha do provedor também podem impedir a conclusão.",
        "Leia os avisos e os limites efetivamente usados. Não significa link inválido."
        " Ajuste o teste conforme o motivo.",
    ),
)

MODEL_EXAMPLES = {
    "g1_daily": (
        "Estratégia de coleta específica para o calendário do índice oficial do g1. "
        "Seleciona todos os arquivos dos dias da janela e da sobreposição. "
        "Caminhos desconhecidos ficam sinalizados para revisão; não inicia varredura histórica.",
        "Índice com milhares de dias → XMLs dos dias selecionados → URLs → hashes em lote. "
        "O teste de saúde continua avaliando acesso e texto sem impor idade à notícia.",
    ),
    "auto": (
        "Quando você ainda não conhece o formato do endereço. Reconhece feed, lista ou"
        " índice e segue filhos declarados; não descobre URLs arbitrárias.",
        "sitemap.xml → índice → noticias.xml → página da notícia",
    ),
    "rss": (
        "Para feeds RSS, Atom ou RDF. Lê as entradas e testa páginas de notícias; não "
        "percorre índices de sitemap.",
        "feed.xml → item/link → página da notícia",
    ),
    "sitemap": (
        "Para uma lista direta de URLs (urlset), incluindo XML comprimido. Um "
        "sitemapindex precisa de outro modelo.",
        "noticias.xml → url/loc → página da notícia",
    ),
    "sitemap_tree": (
        "Para índices com sitemaps filhos e também listas diretas. Deduplica "
        "documentos e respeita profundidade, documentos, tempo e requisições.",
        "sitemap_index.xml → politica.xml → url/loc → notícia",
    ),
    "news_sitemap": (
        "Para uma lista direta com metadados Google News. Não basta ser um sitemap "
        "comum. O formato não impõe que a notícia seja recente.",
        "urlset → url/loc + news:news (título/publicação) → notícia",
    ),
}

PARAMETER_HELP = {
    "updates_overlap_days": (
        "Margem da seleção explícita do índice diário do g1, usada na coleta. "
        "Preserva todas as páginas de cada dia escolhido. O nome do arquivo define "
        "a partição a consultar, sem comprovar a publicação de cada notícia.",
        "Janela de 24/10 e margem de 1 dia: consultar partições de 23 a 25/10 presentes no índice.",
    ),
    "updates_timezone": (
        "Calendário das partições do índice. A janela do usuário mantém seu fuso "
        "e é convertida para selecionar os documentos; não modifica a data das notícias.",
        "Uma janela próxima da meia-noite em Brasília pode alcançar dois dias UTC.",
    ),
    "learn_temporal_routes": (
        "Na coleta, usa famílias de caminhos calibradas contra publicações reais das "
        "amostras mapeadas. Exige várias provas e datas distintas; contraexemplos "
        "invalidam a família. 0 desliga essas inferências. Testes de saúde não usam este corte.",
        "Um arquivo diário antigo pode ser dispensado após aprender a relação entre "
        "sua rota e publicações. Isso é inferência registrada, não garantia de cobertura total.",
    ),
    "archive_overlap_days": (
        "Acrescenta uma margem antes e depois da partição temporal aprendida na coleta. "
        "Não transforma lastmod ou nomes de arquivos em datas comprovadas de publicação.",
        "Com margem de 2 dias, um arquivo próximo da janela continua sendo lido. "
        "Caminhos sem regra validada também continuam elegíveis.",
    ),
    "lookback_days": (
        "0 aceita qualquer data; 365 representa um ano. Nos testes individuais e de "
        "saúde usamos 0 para avaliar funcionamento, independentemente da idade.",
        "0: uma notícia de 2012 pode comprovar funcionamento. A janela de busca não é "
        "a frequência de execução.",
    ),
    "timeout_seconds": (
        "Orçamento total do teste, incluindo XMLs e artigos. O "
        "teste individual e a saúde usam 60 segundos quando Bright Data está ligado.",
        "Com 45 segundos, um índice lento pode consumir o orçamento antes de abrir um "
        "artigo; isso pode deixar o resultado inconclusivo.",
    ),
    "max_requests": (
        "Limita as requisições realizadas no teste, incluindo XMLs, artigos e "
        "redirecionamentos. Funciona junto com os outros limites.",
        "24 requisições não significam 24 notícias: parte é usada em XMLs e redirecionamentos.",
    ),
    "article_samples": (
        "Quantidade máxima de páginas de notícias a conferir. 0 verifica apenas XML e "
        "não comprova texto. Na saúde é garantida ao menos uma amostra.",
        "Uma lista com 100 entradas e amostra 3 permite abrir até três reportagens, "
        "não as 100. Confirmar texto em uma delas já pode comprovar funcionamento. "
        "Amostra maior exige mais tempo e requisições.",
    ),
    "sample_order": (
        "Escolhe posições na ordem de URLs do XML: distribuídas, primeiras ou últimas."
        " Essa ordem não garante ordem cronológica.",
        "Em 100 URLs, distribuídas amostra posições no início, meio e fim; primeiras "
        "não significa mais recentes.",
    ),
    "max_response_mib": (
        "Limite de tamanho por resposta, também aplicado ao XML descomprimido. MiB é "
        "uma medida de tamanho, não uma quantidade de notícias.",
        "Um XML maior que 8 MiB pode atingir o limite mesmo que o arquivo comprimido seja pequeno.",
    ),
    "max_documents": (
        "Máximo de XMLs lidos em modelos que expandem índices. Inclui o documento "
        "inicial e os filhos.",
        "O índice aponta para 50 arquivos de seção. Com limite 12, o teste pode ler a "
        "raiz e até 11 filhos. Os outros 39 não foram necessariamente reprovados: "
        "podem ter ficado fora da amostra por limite.",
    ),
    "max_depth": (
        "Máximo de níveis de XML a partir da raiz, que fica no nível 0. Não é a "
        "quantidade de páginas do site.",
        "Profundidade 2 permite raiz (0), filho (1) e neto (2). O máximo configurável hoje é 10.",
    ),
}

FIELDS = (
    (
        "content-category",
        "Categoria do conteúdo: notícia, opinião e outros tipos",
        "Classifica o que a matéria oferece: notícia/reportagem (news), opinião (opinion), "
        "previsão do tempo (weather), culinária (cooking), esportes (sports), entretenimento "
        "(entertainment), estilo de vida (lifestyle) ou horóscopo (horoscope). "
        "Sem informação suficiente, a categoria fica não classificada (nula).",
        "Uma receita de bolo é cooking. Uma investigação sobre fraude em um restaurante é "
        "news. Uma coluna assinada só é opinion quando o conteúdo expressa opinião; o nome "
        "da seção ou o endereço não decide.",
        "É diferente do estado da execução e da avaliação de risco. Uma matéria esportiva "
        "pode conter um fato adverso. A classificação não apaga notícias nem descarta entidades. "
        "Categorias já gravadas são preservadas; versões antigas podem estar sem classificação.",
    ),
    (
        "article-country",
        "País do artigo e método de identificação",
        "É a origem editorial da publicação, não a nacionalidade das pessoas citadas nem o "
        "país onde aconteceu o fato. A precedência é: região do idioma da página, país "
        "identificado no artigo e país cadastrado da fonte. O método fica salvo como "
        "language, article ou site para explicar a decisão.",
        "Uma reportagem em pt-BR sobre a China recebe BR pelo idioma, mesmo que a fonte "
        "tenha outro país cadastrado. O idioma pt, sem região, não distingue Brasil de Portugal. "
        "Sem país no artigo, usa-se o cadastro da fonte; en-US sozinho também não decide.",
        "A exceção de en-US evita usar o padrão genérico de muitos sites como prova de origem. "
        "Sem evidência válida, o país fica nulo. O artigo só fornece país à análise quando "
        "há trecho explícito sobre sua edição/origem editorial; citar China não basta.",
    ),
    (
        "site",
        "Site / fonte",
        "Registro pai que identifica o veículo e seu endereço. Seus links de busca são"
        " os registros filhos.",
        "Jornal da Cidade → RSS de Política e sitemap de Economia.",
        "Um site pode ter vários links. Inativar o pai impede que seus links sejam "
        "considerados ativos nas rotinas que exigem site ativo.",
    ),
    (
        "link",
        "Link de busca / seção",
        "Endereço do RSS ou sitemap associado a um site e a um mecanismo de leitura. O"
        " nome da seção é apenas uma identificação.",
        "https://jornal.example/politica/feed/ usa RSS / Atom / RDF.",
        "Ter uma seção cadastrada não prova que exista um RSS próprio dela. O teste "
        "precisa comprovar o endereço informado.",
    ),
    (
        "types",
        "RSS, Atom, RDF, sitemap e índice",
        "Feeds publicam entradas; um sitemap direto lista URLs; um índice lista outros"
        " sitemaps. XML é o formato estruturado que permite essa leitura.",
        "RSS: item/link. Sitemap: urlset/url/loc. Índice: sitemapindex/sitemap/loc.",
        "Uma URL XML pode listar páginas que não são notícias; por isso também "
        "testamos artigo e texto.",
    ),
    (
        "cadence",
        "Periodicidade de busca e cadência estimada",
        "Periodicidade é o intervalo cadastrado para consultar o link. Quando a busca usa "
        "horários fixos, esse intervalo individual não controla as rodadas. Cadência é uma "
        "estimativa de publicação obtida das datas disponíveis, quando há dados "
        "suficientes.",
        "240 minutos = 4 horas. Uma estimativa de 30 minutos sugere publicações "
        "frequentes, mas não é garantia.",
        "Sem periodicidade definida, o padrão é 240 minutos. O ajuste dos valores "
        "existentes para 4 horas não é um teto geral: o intervalo continua editável.",
    ),
    (
        "active",
        "Ativo, inativo e excluído",
        "São estados do cadastro, separados do resultado do teste. Excluir retira o "
        "registro do gerenciador, preservando histórico por exclusão lógica.",
        "Um link pode estar ativo e ter uma falha recente; um link inativo pode voltar"
        " a entregar texto.",
        "Um teste saudável não ativa automaticamente por si só. A recuperação só ativa"
        " quando a opção está habilitada e as condições de segurança são satisfeitas.",
    ),
    (
        "history",
        "Evidência, histórico e revisão",
        "Evidência registra o que foi comprovado e quando. A revisão identifica a "
        "versão do cadastro ou mecanismo usada no teste.",
        "A revisão 2 de um modelo pode ter limites diferentes da revisão 1 usada num teste antigo.",
        "Alterar URL, tipo, modelo ou configuração pode exigir nova comprovação. "
        "Testes em andamento não devem sobrescrever alterações posteriores do "
        "cadastro.",
    ),
    (
        "sample",
        "Amostra e URLs encontradas",
        "URLs encontradas são candidatas obtidas do XML. Amostras são as páginas "
        "efetivamente abertas para conferir notícia e texto.",
        "5.000 URLs encontradas e três amostras não significam 5.000 notícias extraídas.",
        "Uma amostra positiva comprova o caminho testado. Não mede cobertura de 100% "
        "do site nem garante que todos os filhos foram lidos.",
    ),
    (
        "proof",
        "XML comprovado, histórico e Não registrado",
        "XML comprovado indica evidência de leitura do documento. Histórico remete a "
        "comprovações anteriores. Não registrado indica que aquela informação não "
        "consta da evidência disponível.",
        "Podemos ter confirmação antiga do RSS sem uma contagem registrada de artigos extraídos.",
        "XML comprovado não equivale a notícia textual confirmada. Não registrado é "
        "informação ausente, não zero e nem falha comprovada.",
    ),
    (
        "text",
        "Notícia textual, áudio e texto útil",
        "A confirmação exige conteúdo de reportagem aproveitável na página. Uma "
        "resposta HTTP 200, um título ou a descrição curta do feed não bastam.",
        "Um episódio de podcast sem reportagem textual não serve ao objetivo de busca "
        "de notícias textuais.",
        "Paywall, JavaScript e limitações do extrator podem impedir a comprovação "
        "mesmo com texto no navegador.",
    ),
    (
        "transport",
        "Acesso direto, fallback e Bright Data",
        "O teste tenta acesso direto primeiro. Quando habilitado, Bright Data é "
        "alternativa paga para determinados bloqueios e falhas de rede.",
        "HTTP direto 403 → fallback → XML e artigo → extração textual.",
        "Não é usado para ausência de texto, áudio, XML vazio, 404 ou autenticação "
        "401. O acesso é direto, sem consulta a robots.txt. Chamadas são "
        "contadas; o painel não calcula a fatura.",
    ),
)

HEALTH = (
    (
        "active_round",
        "Rotina de links ativos",
        "Roda às 10h e às 20h. Lê o documento de cada link ativo (sem baixar notícias) e "
        "decide: manter, inativar (parado há mais de 1 mês ou quebrado), excluir (1 ano "
        "ou mais sem notícia, período encerrado ou tema inútil) ou deixar para sua revisão.",
        "Um feed que não publica há 45 dias é inativado; o sitemap de um dia passado é "
        "excluído; um feed de esportes aparece para você revisar.",
        "Falha passageira (tempo esgotado, erro do site) só inativa na segunda verificação "
        "seguida; 404, 410 e ciclo de redirecionamento inativam na hora. Antes de inativar "
        "por bloqueio, a rotina tenta outros jeitos de conectar e troca o transporte do link "
        "se um funcionar. É independente da busca de notícias e do intervalo de cada link.",
    ),
    (
        "recovery_round",
        "Rotina de recuperação",
        "Testa os links inativos às 10h e às 20h e só reativa quando há notícia textual "
        "comprovada e publicação nos últimos 30 dias. Na rodada agendada, retesta a cada "
        "vez quem a própria saúde inativou na última semana; os demais, a cada 7 dias. "
        "Escopos ampliados e a opção Executar agora testam tudo o que foi escolhido.",
        "Escolher todos os inativos visíveis ou excluídos automaticamente por falha de acesso.",
        "Um site que responde bem mas só tem notícias antigas continua inativo. "
        "Exclusões e inativações manuais não são desfeitas pela recuperação de "
        "exclusões automáticas. Um pai inativado pela validação por ficar vazio pode "
        "voltar após comprovação textual.",
    ),
    (
        "interval_minutes",
        "Repetir a cada (minutos)",
        "Intervalo entre verificações de saúde desta rotina. Não é a periodicidade de "
        "busca configurada nos links.",
        "1440 minutos = 24 horas. 240 minutos = 4 horas.",
        "Só vale quando a rotina não tem horários fixos; com horários (hoje 10h e 20h) "
        "eles mandam. O serviço do gerenciador precisa estar ligado. Fechar o navegador não "
        "interrompe o serviço; desligar o computador interrompe a execução até a "
        "retomada.",
    ),
    (
        "workers",
        "Sites em paralelo (workers)",
        "Quantidade máxima de testes simultâneos. A fila evita concorrência entre "
        "itens agrupados pelo mesmo host da fonte.",
        "8 permite testar fontes diferentes em paralelo, respeitando a disponibilidade da fila.",
        "Aumentar o valor pode ampliar o uso de rede e de chamadas pagas; não elimina "
        "os limites de cada teste.",
    ),
    (
        "max_attempts",
        "Tentativas por link",
        "Máximo de tentativas na rodada. O teste só repete automaticamente quando a "
        "categoria é falha de acesso.",
        "2 permite uma tentativa inicial e uma repetição de acesso, sem uma busca indefinida.",
        "Não significa testar todos os mecanismos disponíveis. A rotina usa o "
        "mecanismo atribuído ao link.",
    ),
    (
        "max_bright_requests",
        "Chamadas Bright Data por teste",
        "Teto de chamadas pagas ao fallback em cada tentativa, além do orçamento geral"
        " de rede e tempo.",
        "8 chamadas por tentativa e até duas tentativas podem totalizar até 16 por "
        "link, se os demais limites permitirem.",
        "A contagem da rodada soma todas as tentativas. O limite atingido pode deixar "
        "a comprovação inconclusiva; ele não é um valor em dólares.",
    ),
    (
        "activate_recovered",
        "Ativar os links recuperados",
        "Autoriza a rotina de recuperação a ativar links com notícia textual "
        "comprovada e cadastro compatível com a revisão congelada.",
        "Um link excluído automaticamente por acesso é restaurado somente depois de "
        "entregar texto útil.",
        "Sem esta opção, o teste registra a saúde, mas não ativa o cadastro. Links sem"
        " notícias ou sem texto não são ativados.",
    ),
    (
        "enabled",
        "Execução periódica habilitada e Executar agora",
        "Habilitar mantém o agendamento. Executar agora cria uma rodada manual usando "
        "as configurações salvas e o escopo selecionado.",
        "Depois de alterar limites, use Salvar configuração antes de Executar agora.",
        "Pausar o agendamento não cancela uma rodada já aberta. Use a ação de cancelar"
        " na execução para interromper os itens restantes.",
    ),
    (
        "scope",
        "Escopo e fila congelada",
        "Escopo define quais links entram na rodada. A lista é congelada no início e "
        "novas descobertas não aumentam a fila.",
        "Excluídos automaticamente por falha de acesso seleciona decisões auditadas da"
        " validação, não exclusões manuais.",
        "Escopos manuais: inativos com falha de acesso; todos os inativos visíveis; "
        "excluídos automaticamente por falha de acesso. O agendamento continua "
        "restrito ao primeiro.",
    ),
)

STATES = (
    (
        "queued",
        "Na fila / Pendente",
        "A rodada ou o link aguarda sua vez; ainda não é um resultado de saúde.",
        "Há fontes em execução e os demais itens aguardam.",
        "Aguarde o processamento ou consulte os itens já concluídos.",
    ),
    (
        "running",
        "Em execução",
        "A rotina está processando a fila, ou o link está em teste.",
        "Acesso ao XML, expansão de filhos ou teste de artigo em andamento.",
        "A quantidade de concluídos pode ficar estável enquanto os testes atuais "
        "aguardam respostas.",
    ),
    (
        "completed",
        "Concluída",
        "A rodada terminou de processar sua fila. Isso não significa que todos os "
        "links funcionaram.",
        "Uma execução concluída pode conter saudáveis, falhas e inconclusivos.",
        "Consulte a categoria de cada link e o selo Ativado para distinguir "
        "confirmação de mudança no cadastro.",
    ),
    (
        "cancelled",
        "Cancelando / Cancelada",
        "Foi solicitada a interrupção. Itens em curso encerram dentro de seus limites;"
        " os demais deixam de ser distribuídos.",
        "Os resultados concluídos antes do cancelamento continuam no histórico.",
        "Cancelamento não desfaz ativações ou resultados já gravados.",
    ),
    (
        "skipped",
        "Cadastro alterado",
        "O cadastro ou mecanismo mudou desde o início do teste. O resultado não deve "
        "sobrescrever essa edição.",
        "A URL de um link foi alterada enquanto a rotina ainda usava a URL anterior.",
        "Teste novamente a configuração atual. Não é reprovação do site.",
    ),
    (
        "error",
        "Falha interna",
        "O processo encontrou um erro interno ao executar o teste, sem uma conclusão "
        "confiável sobre a fonte.",
        "Uma exceção impediu finalizar a verificação do link.",
        "Confira o diagnóstico técnico antes de atribuir falha ao site.",
    ),
    (
        "activated",
        "Ativado após recuperação",
        "A rotina aplicou a mudança de cadastro após comprovar notícia textual e "
        "conferir as revisões.",
        "O teste saudável restaurou um link antes excluído automaticamente por acesso.",
        "Nem todo saudável terá este selo: o cadastro pode já estar ativo ou a "
        "ativação automática pode estar desabilitada.",
    ),
)


def card(key: str, title: str, meaning: str, example: str, action: str) -> str:
    """Renderiza conteúdo de texto com escape; os exemplos não executam HTML."""
    return (
        f'<article class="entry" id="termo-{escape(key)}"><h3>{escape(title)}</h3>'
        f'<p>{escape(meaning)}</p><p class="example"><strong>Exemplo:</strong> '
        f'{escape(example)}</p><p class="interpret"><strong>Como interpretar:</strong> '
        f"{escape(action)}</p></article>"
    )


def render_guide() -> bytes:
    """Usa o registro dos mecanismos para manter nomes, padrões e limites alinhados."""
    sections = {"results": "".join(card(*r) for r in RESULTS)}
    sections["fields"] = "".join(card(*r) for r in FIELDS)
    sections["health"] = "".join(card(*r) for r in HEALTH)
    sections["states"] = "".join(card(*r) for r in STATES)
    sections["models"] = ""
    parameters = {}
    for mechanism in MECHANISMS.values():
        meaning, example = MODEL_EXAMPLES[mechanism.id]
        sections["models"] += card(
            "model-" + mechanism.id,
            mechanism.label,
            mechanism.description + " " + meaning,
            example,
            "O endereço deve entregar o formato compatível com este leitor. Uma URL que "
            "termina em .xml não garante isso: confira o resultado de Testar funcionamento.",
        )
        for parameter in mechanism.parameters:
            parameters[parameter.key] = parameter
    sections["parameters"] = ""
    for key, parameter in parameters.items():
        meaning, example = PARAMETER_HELP[key]
        limits = (
            "Opções: " + "; ".join(text for _, text in parameter.choices) + "."
            if parameter.choices
            else f"Faixa permitida: {parameter.minimum} a {parameter.maximum}."
        )
        sections["parameters"] += card(
            "param-" + key,
            parameter.label,
            meaning,
            example,
            f"Padrão do modelo: {parameter.default}. {limits} "
            "Os valores salvos podem ser diferentes: consulte Mecanismos de busca. "
            "Alterações globais afetam os próximos testes dos links que usam esse modelo. "
            "Não alteram os resultados antigos já registrados.",
        )
    template = resources.files("buscador_midias.dashboard").joinpath("guide.html").read_text()
    for key, content in sections.items():
        template = template.replace("{{" + key + "}}", content)
    return template.encode()
