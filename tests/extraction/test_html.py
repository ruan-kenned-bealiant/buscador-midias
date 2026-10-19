from datetime import UTC, datetime

from buscador_midias.extraction.html import extract_article

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

PARAGRAPH = (
    "A Polícia Federal prendeu o empresário na manhã desta quarta-feira, em Belo Horizonte. "
)


def page(head: str = "", body: str = "", lang: str = "pt-BR") -> str:
    return f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">{head}</head><body>{body}</body></html>'


def article_body(n: int = 6) -> str:
    return (
        "<article><h1>PF prende empresário</h1>"
        + "".join(f"<p>{PARAGRAPH}</p>" for _ in range(n))
        + "</article>"
    )


def test_titulo_prefere_og_title_depois_title_depois_h1():
    og = page(
        '<meta property="og:title" content="Titulo OG"><title>Titulo tag - Veículo</title>',
        article_body(),
    )
    tag = page("<title>Titulo tag - Veículo</title>", article_body())
    h1 = page("", article_body())

    assert extract_article(og, now=NOW).title == "Titulo OG"
    assert extract_article(tag, now=NOW).title == "Titulo tag - Veículo"
    assert extract_article(h1, now=NOW).title == "PF prende empresário"


def test_descricao_vem_do_og_ou_da_meta_description():
    og = page('<meta property="og:description" content="Resumo OG">', article_body())
    meta = page('<meta name="description" content="Resumo meta">', article_body())

    assert extract_article(og, now=NOW).description == "Resumo OG"
    assert extract_article(meta, now=NOW).description == "Resumo meta"
    assert extract_article(page("", article_body()), now=NOW).description is None


def test_texto_vem_dos_paragrafos_do_artigo_sem_menu_rodape_e_scripts():
    html = page(
        "<script>var x = 1;</script><style>p{color:red}</style>",
        "<nav><p>Menu principal com bastante texto de navegação do site inteiro</p></nav>"
        + article_body(3)
        + "<footer><p>Todos os direitos reservados ao veículo, política de privacidade e termos</p></footer>",
    )

    content = extract_article(html, now=NOW)

    assert "Polícia Federal prendeu" in content.text
    assert "Menu principal" not in content.text
    assert "direitos reservados" not in content.text
    assert "var x" not in content.text


def test_texto_e_limitado_mas_o_tamanho_total_e_informado():
    content = extract_article(page("", article_body(40)), now=NOW, max_chars=500)

    assert len(content.text) <= 500
    assert content.text_chars > 3000


def test_sem_tag_article_usa_main_ou_paragrafos_longos_do_corpo():
    html = page(
        "",
        "<main>"
        + "".join(f"<p>{PARAGRAPH}</p>" for _ in range(4))
        + "</main><div><p>curto</p></div>",
    )

    content = extract_article(html, now=NOW)

    assert content.text.count("Polícia Federal") == 4
    assert "curto" not in content.text


def test_json_ld_article_body_e_usado_quando_o_html_nao_tem_paragrafos():
    body_text = PARAGRAPH * 6
    ld = f'<script type="application/ld+json">{{"@type":"NewsArticle","articleBody":"{body_text}"}}</script>'

    content = extract_article(page(ld, "<div id=app></div>"), now=NOW)

    assert content.text.startswith("A Polícia Federal prendeu")
    assert content.has_article_data is True


def test_data_prefere_meta_depois_json_ld_depois_time():
    meta = page(
        '<meta property="article:published_time" content="2026-10-14T10:30:00-03:00">',
        article_body(),
    )
    ld = page(
        '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","datePublished":"2026-10-13T09:00:00Z"}]}</script>',
        article_body(),
    )
    time_tag = page("", article_body() + '<time datetime="2026-10-12T08:00:00Z">ontem</time>')

    assert extract_article(meta, now=NOW).published_at == datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    assert extract_article(ld, now=NOW).published_at == datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    assert extract_article(time_tag, now=NOW).published_at == datetime(
        2026, 10, 12, 8, 0, tzinfo=UTC
    )


def test_data_invalida_antiga_ou_futura_e_descartada():
    for value in ("0000-00-00", "1985-01-01T00:00:00Z", "2027-01-01T00:00:00Z", "lixo"):
        html = page(f'<meta property="article:published_time" content="{value}">', article_body())

        assert extract_article(html, now=NOW).published_at is None


def test_data_sem_fuso_vale_como_utc():
    html = page(
        '<meta property="article:published_time" content="2026-10-14T10:30:00">', article_body()
    )

    assert extract_article(html, now=NOW).published_at == datetime(2026, 10, 14, 10, 30, tzinfo=UTC)


def test_idioma_vem_do_html_lang_ou_og_locale_e_so_se_curto():
    assert extract_article(page("", article_body(), lang="pt-BR"), now=NOW).language == "pt-BR"
    no_lang = '<html><head><meta property="og:locale" content="en_US"></head><body></body></html>'
    assert extract_article(no_lang, now=NOW).language == "en-US"
    assert extract_article(page("", article_body(), lang="x" * 20), now=NOW).language is None


def test_url_canonica_e_sinal_de_artigo():
    html = page('<link rel="canonical" href="https://a.com/materia">', article_body())

    content = extract_article(html, now=NOW)

    assert content.canonical_url == "https://a.com/materia"
    assert content.has_article_data is True  # tag <article>
    assert extract_article(page("", "<div><p>oi</p></div>"), now=NOW).has_article_data is False


def test_bytes_com_charset_declarado_sao_decodificados():
    html = (
        '<html><head><meta charset="iso-8859-1"></head><body><article><h1>Ação</h1>'
        + f"<p>{PARAGRAPH}</p>" * 3
        + "</article></body></html>"
    )

    content = extract_article(html.encode("iso-8859-1"), now=NOW)

    assert content.title == "Ação"


def test_html_quebrado_ou_vazio_nunca_levanta():
    for bad in ("", "<<<>>>", "\x00\x01", b"\xff\xfe\x00", "<html><body><p>"):
        content = extract_article(bad, now=NOW)

        assert content.text == "" or isinstance(content.text, str)
        assert content.published_at is None


def test_e_artigo_com_json_ld_de_artigo_og_type_ou_data_de_meta():
    ld = page(
        '<script type="application/ld+json">{"@type":"NewsArticle","headline":"x"}</script>',
        article_body(),
    )
    og = page('<meta property="og:type" content="article">', article_body())
    meta_date = page(
        '<meta property="article:published_time" content="2026-10-14T10:30:00Z">', article_body()
    )

    assert extract_article(ld, now=NOW).is_article is True
    assert extract_article(og, now=NOW).is_article is True
    assert extract_article(meta_date, now=NOW).is_article is True


def test_pagina_de_secao_nao_e_artigo_mesmo_com_tag_article_e_time_nos_destaques():
    teasers = "".join(
        f'<article><h2>Destaque {n}</h2><time datetime="2026-10-14T0{n}:00:00Z">hoje</time>'
        f"<p>{PARAGRAPH}</p></article>"
        for n in range(1, 5)
    )

    content = extract_article(page('<meta property="og:type" content="website">', teasers), now=NOW)

    assert content.is_article is False
    assert (
        content.has_article_data is True
    )  # a tag <article> existe, mas não prova que é uma matéria


def test_pagina_sem_nenhum_sinal_nao_e_artigo():
    assert extract_article(page("", article_body()), now=NOW).is_article is False


def test_meta_de_publicacao_de_cms_conta_como_data_e_como_sinal_de_artigo():
    head = '<meta name="cXenseParse:recs:publishtime" content="2026-10-14T10:30:00-03:00">'

    content = extract_article(page(head, article_body()), now=NOW)

    assert content.published_at == datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    assert content.is_article is True


def test_meta_de_data_que_nao_e_de_publicacao_nao_conta():
    head = '<meta name="last-updated-widget" content="2026-10-14T10:30:00Z"><meta name="description" content="2026-10-14">'

    content = extract_article(page(head, article_body()), now=NOW)

    assert content.is_article is False


def test_data_dos_metas_padrao_tem_prioridade_sobre_a_generica():
    head = (
        '<meta name="cXenseParse:recs:publishtime" content="2026-10-01T00:00:00Z">'
        '<meta property="article:published_time" content="2026-10-14T10:30:00Z">'
    )

    assert extract_article(page(head, article_body()), now=NOW).published_at == datetime(
        2026, 10, 14, 10, 30, tzinfo=UTC
    )


from buscador_midias.extraction.html import decode_html  # noqa: E402


def utf8_page(title: str = "Polícia prende empresário em São Paulo") -> bytes:
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        + f"<p>{PARAGRAPH}</p>" * 4
        + "</article></body></html>"
    ).encode("utf-8")


def test_utf8_sem_charset_declarado_nao_vira_mojibake():
    content = extract_article(utf8_page(), now=NOW)

    assert content.title == "Polícia prende empresário em São Paulo"
    assert "Ã" not in content.text
    assert "Polícia Federal" in content.text


def test_utf8_valido_vence_um_charset_errado_declarado_no_cabecalho():
    html = utf8_page()

    assert extract_article(html, now=NOW, content_type="text/html; charset=iso-8859-1").title == (
        "Polícia prende empresário em São Paulo"
    )


def test_bytes_que_nao_sao_utf8_usam_o_charset_do_cabecalho_quando_nao_ha_meta():
    html = (
        "<html><head><title>Ação</title></head><body><article><h1>Ação</h1></article></body></html>"
    )

    content = extract_article(
        html.encode("iso-8859-1"), now=NOW, content_type="text/html; charset=iso-8859-1"
    )

    assert content.title == "Ação"


def test_bytes_que_nao_sao_utf8_e_sem_declaracao_caem_no_windows_1252():
    html = "<html><head><title>Ação</title></head><body></body></html>".encode("cp1252")

    assert extract_article(html, now=NOW).title == "Ação"


def test_bom_utf8_e_utf16_sao_reconhecidos():
    base = "<html><head><title>Ação</title></head><body></body></html>"

    assert extract_article(b"\xef\xbb\xbf" + base.encode("utf-8"), now=NOW).title == "Ação"
    assert extract_article(base.encode("utf-16"), now=NOW).title == "Ação"


def test_charset_desconhecido_no_cabecalho_nao_levanta():
    assert decode_html("Ação".encode(), "text/html; charset=nao-existe") == "Ação"
    assert decode_html(b"abc", None) == "abc"
    assert decode_html(b"", None) == ""


def test_texto_em_divs_com_br_dentro_do_artigo_tambem_e_extraido():
    lines = "<br>".join(PARAGRAPH for _ in range(6))
    html = page("", f"<article><h1>Titulo</h1><div class='corpo'>{lines}</div></article>")

    content = extract_article(html, now=NOW)

    assert content.text_chars > 400
    assert "Polícia Federal prendeu" in content.text


def test_texto_sem_article_nem_main_cai_para_o_corpo_sem_menu_e_rodape():
    body = (
        "<nav>Início Política Economia Esportes Cultura Mais notícias do site inteiro</nav>"
        f"<div class='post'>{PARAGRAPH * 5}</div><footer>Todos os direitos reservados</footer>"
    )

    content = extract_article(page("", body), now=NOW)

    assert "Polícia Federal prendeu" in content.text
    assert "Início Política" not in content.text
    assert "direitos reservados" not in content.text


def test_paragrafos_bons_continuam_sendo_a_fonte_do_texto_sem_lixo_do_contêiner():
    body = (
        "<article><p>"
        + PARAGRAPH * 3
        + "</p><div class='relacionadas'>Leia também: outra matéria qualquer do site</div></article>"
    )

    content = extract_article(page("", body), now=NOW)

    assert "Leia também" not in content.text


def test_proporcao_de_caracteres_cjk():
    from buscador_midias.extraction.html import cjk_ratio

    assert cjk_ratio("") == 0.0
    assert cjk_ratio("only latin text") == 0.0
    assert cjk_ratio("これは日本語の文章です") > 0.8
    assert 0.3 < cjk_ratio("abc 日本語 def 日本語") < 0.7


def test_data_que_transborda_utc_nao_interrompe_extracao_da_noticia():
    for value in ("0001-01-01T00:00:00+04:00", "9999-12-31T23:59:59-04:00"):
        source = page(
            '<meta property="og:type" content="article">'
            f'<meta property="article:published_time" content="{value}">',
            article_body(),
        )
        content = extract_article(source, now=NOW)
        assert content.title == "PF prende empresário"
        assert content.text_chars > 300 and content.is_article
        assert content.published_at is None
