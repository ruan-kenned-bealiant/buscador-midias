from datetime import UTC, datetime

import pytest

from buscador_midias.extraction.reliability import (
    PageSnapshot,
    assess_evidence,
    looks_like_challenge_page,
    looks_like_error_page,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
URL = "https://g1.globo.com/politica/noticia/2026/10/15/pf-prende-empresario-por-fraude-em-licitacao.ghtml"
HEADLINE = "pf prende empresario por fraude em licitacao"
TEXT = (
    "A Polícia Federal prendeu o empresário João Silva nesta quarta-feira por suspeita de fraude "
    "em licitação em contratos da prefeitura. Segundo a investigação, o esquema desviou recursos. "
)


def html(
    *,
    title: str = "PF prende empresário por fraude em licitação - G1",
    paragraphs: int = 8,
    extra_body: str = "",
    extra_head: str = "",
) -> bytes:
    body = "".join(f"<p>{TEXT}</p>" for _ in range(paragraphs))
    return (
        f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>'
        '<meta property="og:type" content="article">'
        f"{extra_head}</head><body><article><h1>{title}</h1>{body}</article>{extra_body}</body></html>"
    ).encode()


def page(content: bytes | None = None, **overrides) -> PageSnapshot:
    base = {
        "url": URL,
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "final_url": URL,
        "html": content if content is not None else html(),
    }
    base.update(overrides)
    return PageSnapshot(**base)


def assess(snapshot: PageSnapshot, **kwargs):
    kwargs.setdefault("expected_headline", HEADLINE)
    kwargs.setdefault("now", NOW)
    return assess_evidence(snapshot, **kwargs)


def test_pagina_integra_e_confiavel():
    result = assess(page())

    assert result.ok is True
    assert result.reasons == ()


@pytest.mark.parametrize("status", [None, 301, 403, 404, 500])
def test_so_status_200_serve(status):
    assert "http_status" in assess(page(status=status)).reasons


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/json"])
def test_tipo_de_conteudo_precisa_ser_html(content_type):
    assert "not_html" in assess(page(content_type=content_type)).reasons


def test_tipo_de_conteudo_ausente_e_aceito_se_o_corpo_parece_html():
    assert assess(page(content_type=None)).ok is True


def test_pagina_minuscula_nao_serve():
    assert "too_small" in assess(page(b"<html><body>oi</body></html>")).reasons


def test_pagina_de_erro_com_status_200_e_recusada():
    soft_404 = html(title="Página não encontrada - 404 | G1", paragraphs=8)

    assert "error_page" in assess(page(soft_404)).reasons


def test_pagina_de_desafio_do_bot_e_recusada():
    challenge = (
        b"<html><head><title>Just a moment...</title></head><body><h1>Checking your browser</h1>"
        b"<p>Enable JavaScript and cookies to continue</p>"
        + b"<div></div>" * 400
        + b"</body></html>"
    )

    reasons = assess(page(challenge)).reasons

    assert "challenge_page" in reasons


def test_palavra_de_desafio_dentro_de_matemoria_longa_nao_dispara():
    long_article = html(extra_body="", paragraphs=30).replace(
        b"suspeita de fraude", b"suspeita de fraude, e o captcha da prefeitura"
    )

    assert "challenge_page" not in assess(page(long_article)).reasons


def test_redirecionamento_para_outro_site_e_recusado():
    assert "redirected_away" in assess(page(final_url="https://outro-site.com/materia")).reasons


def test_redirecionamento_para_a_home_e_recusado():
    assert "redirected_away" in assess(page(final_url="https://g1.globo.com/")).reasons


def test_redirecionamento_no_mesmo_site_e_para_amp_ou_www_e_aceito():
    same_site = "https://www.g1.globo.com/politica/noticia/2026/10/15/pf-prende-empresario.ghtml"

    assert "redirected_away" not in assess(page(final_url=same_site)).reasons


def test_pagina_de_outra_materia_e_recusada_pelo_titulo():
    other = html(title="Previsão do tempo para o fim de semana - G1")

    assert "title_mismatch" in assess(page(other)).reasons


def test_titulo_diferente_mas_com_a_mesma_materia_passa():
    reworded = html(title="Empresário é preso pela PF por fraude em licitação | G1")

    assert "title_mismatch" not in assess(page(reworded)).reasons


def test_sem_manchete_esperada_nao_confere_o_titulo():
    assert "title_mismatch" not in assess(page(), expected_headline="").reasons
    assert "title_mismatch" not in assess(page(), expected_headline="curto demais").reasons


def test_texto_curto_e_considerado_conteudo_fino():
    assert "thin_content" in assess(page(html(paragraphs=1))).reasons
    assert "thin_content" not in assess(page(html(paragraphs=8))).reasons
    assert "thin_content" in assess(page(html(paragraphs=3)), min_text_chars=2000).reasons


def test_paywall_com_texto_curto_e_recusado():
    paywalled = html(
        paragraphs=3, extra_body="<div>Assine para continuar lendo esta reportagem</div>"
    )

    assert "paywall" in assess(page(paywalled)).reasons


def test_aviso_de_assinatura_em_materia_completa_nao_e_paywall():
    full = html(
        paragraphs=30, extra_body="<div>Assine a newsletter</div><div>assine para continuar</div>"
    )

    assert "paywall" not in assess(page(full)).reasons


def test_entidade_da_analise_precisa_aparecer_na_pagina():
    assert "entity_missing" not in assess(page(), must_mention=["João Silva"]).reasons
    assert (
        "entity_missing" not in assess(page(), must_mention=["Fulano de Tal", "JOAO SILVA"]).reasons
    )
    assert "entity_missing" in assess(page(), must_mention=["Fulano de Tal"]).reasons
    assert "entity_missing" not in assess(page(), must_mention=[]).reasons


def test_varios_motivos_se_acumulam_na_ordem_dos_criterios():
    result = assess(page(b"<html><body>404 not found</body></html>", status=404))

    assert result.ok is False
    assert result.reasons[0] == "http_status"
    assert "too_small" in result.reasons


def test_detector_de_pagina_de_erro_ignora_acento_e_caixa():
    assert (
        looks_like_error_page(
            "<html><head><title>Página NÃO encontrada</title></head><body></body></html>"
        )
        is True
    )
    assert (
        looks_like_error_page(
            "<html><head><title>Materia boa</title></head><body>texto normal</body></html>"
        )
        is False
    )


def test_detector_de_erro_exige_dois_sinais_no_corpo_ou_um_com_titulo_ausente():
    two = "<html><head><title>Algo</title></head><body>404 e tambem page not found</body></html>"
    one_with_expected_missing = (
        "<html><head><title>Algo</title></head><body>conteudo nao encontrado</body></html>"
    )
    article_ld = (
        '<html><head><title>Algo</title><script type="application/ld+json">{"@type":"NewsArticle"}</script>'
        "</head><body>conteudo nao encontrado</body></html>"
    )

    assert looks_like_error_page(two) is True
    assert looks_like_error_page(one_with_expected_missing, expected_title="Outra Coisa") is True
    assert looks_like_error_page(article_ld, expected_title="Outra Coisa") is False


def test_detector_de_desafio():
    assert looks_like_challenge_page("Attention Required! | Cloudflare", text_chars=100) is True
    assert looks_like_challenge_page("Noticia comum", text_chars=5000) is False


def test_pagina_sem_sinais_de_artigo_e_recusada_mesmo_com_texto_suficiente():
    section = (
        b'<!doctype html><html><head><meta charset="utf-8"><title>Sindh</title></head><body>'
        + b"".join(f"<p>{TEXT}</p>".encode() for _ in range(8))
        + b"</body></html>"
    )

    reasons = assess(page(section), expected_headline="").reasons

    assert "not_article" in reasons


def test_pagina_utf8_sem_charset_nao_falha_por_mojibake_na_checagem_de_entidade():
    content = html().replace(
        b'<meta charset="utf-8">', b""
    )  # sem charset: o lxml assumiria latin-1

    result = assess(page(content), must_mention=["João Silva"])

    assert "entity_missing" not in result.reasons
    assert result.ok is True


def test_cabecalho_com_charset_e_usado_pelo_avaliador():
    latin = html().replace(b'<meta charset="utf-8">', b"").decode("utf-8").encode("iso-8859-1")

    result = assess(
        page(latin, content_type="text/html; charset=iso-8859-1"), must_mention=["João Silva"]
    )

    assert "entity_missing" not in result.reasons


def cjk_page(chars: int) -> bytes:
    body = "".join(f"<p>{'日本語の記事の本文です。' * 3}</p>" for _ in range(max(chars // 36, 1)))
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>首相が会見</title>'
        '<meta property="og:type" content="article"></head>'
        f"<body><article><h1>首相が会見</h1>{body}</article></body></html>"
    ).encode()


def test_limite_de_texto_menor_para_cjk_onde_cada_caractere_carrega_mais():
    short_ja = page(cjk_page(250), url="https://exemplo.jp/a/1", final_url="https://exemplo.jp/a/1")

    reasons = assess(short_ja, expected_headline="").reasons

    assert "thin_content" not in reasons
    assert (
        "thin_content" in assess(page(html(paragraphs=1)), expected_headline="").reasons
    )  # latim curto


def test_cjk_muito_curto_continua_fino():
    tiny = page(cjk_page(36), url="https://exemplo.jp/a/1", final_url="https://exemplo.jp/a/1")

    assert "thin_content" in assess(tiny, expected_headline="").reasons


def article_with(title: str, canonical: str | None = None, text: str = TEXT) -> bytes:
    head = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta property="og:type" content="article">{head}</head><body><article><h1>{title}</h1>'
        + "".join(f"<p>{text}</p>" for _ in range(8))
        + "</article></body></html>"
    ).encode()


def test_titulo_em_outro_script_nao_e_comparado_com_slug_transliterado():
    url = (
        "https://wearesolomon.com/el/ektopismenoi-tis-thessalias-pou-zoun-dyo-xronia-se-conteiner/"
    )
    greek = article_with("«Μετανάστες στον τόπο τους»: Οι εκτοπισμένοι της Θεσσαλίας")

    result = assess(
        page(greek, url=url, final_url=url),
        expected_headline="ektopismenoi tis thessalias pou zoun dyo xronia",
    )

    assert "title_mismatch" not in result.reasons


def test_titulo_em_cirilico_ou_chines_tambem_nao_e_comparado():
    for title in (
        "Ексчоловік блогерки Скальницької, молодший інспектор поліції",
        "【事實查核】烏克蘭總統顧問沒有說過這句話",
    ):
        result = assess(
            page(article_with(title)),
            expected_headline="eksholovik blogerki skalnitskoi molodshii inspektor politsii",
        )

        assert "title_mismatch" not in result.reasons, title


def test_canonica_igual_a_url_pedida_prova_que_e_a_materia_certa():
    other_title = article_with("Título totalmente diferente sobre outro assunto", canonical=URL)

    assert "title_mismatch" not in assess(page(other_title)).reasons


def test_canonica_de_outra_materia_nao_prova_nada_e_o_titulo_decide():
    other = article_with(
        "Previsão do tempo para o fim de semana",
        canonical="https://g1.globo.com/outra/materia.ghtml",
    )

    assert "title_mismatch" in assess(page(other)).reasons


def test_titulo_latino_de_outra_materia_continua_reprovando():
    assert (
        "title_mismatch"
        in assess(page(article_with("Previsão do tempo para o fim de semana"))).reasons
    )


def test_slug_com_letra_nao_ascii_codificada_na_url_e_comparado_por_letras_unicode():
    url = "https://meydan.tv/az/avropa-m%C9%99hk%C9%99m%C9%99si-iki-az%C9%99rbaycanl%C4%B1-f%C9%99alin-huquqlarinin-pozuldugunu"
    title = "Avropa Məhkəməsi iki azərbaycanlı fəalın hüquqlarının pozulduğunu tanıyıb"

    result = assess(
        page(article_with(title), url=url, final_url=url),
        expected_headline="avropa məhkəməsi iki azərbaycanlı fəalın hüquqlarının pozulduğunu",
    )

    assert "title_mismatch" not in result.reasons
