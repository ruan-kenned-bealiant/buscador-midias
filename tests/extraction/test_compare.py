from datetime import UTC, datetime

from buscador_midias.extraction.compare import compare_pages

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TEXT = (
    "A Polícia Federal prendeu o empresário João Silva nesta quarta-feira por suspeita de fraude "
    "em licitação em contratos da prefeitura. Segundo a investigação, o esquema desviou recursos. "
)


def page(
    text: str = TEXT,
    title: str = "PF prende empresário",
    date: str = "2026-10-14T10:00:00Z",
    lang: str = "pt-BR",
) -> bytes:
    body = "".join(f"<p>{text}</p>" for _ in range(8))
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta property="article:published_time" content="{date}"></head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    ).encode()


def test_mesma_pagina_e_identica():
    result = compare_pages(page(), page(), now=NOW)

    assert result.text_similarity == 1.0
    assert (result.same_title, result.same_published_at, result.same_language) == (True, True, True)
    assert result.length_ratio == 1.0


def test_paginas_diferentes_tem_baixa_similaridade_e_campos_diferentes():
    other = page(
        "O prefeito inaugurou a nova praça no centro da cidade com moradores e artistas locais. ",
        title="Prefeito inaugura praça",
        date="2026-10-10T10:00:00Z",
        lang="en",
    )

    result = compare_pages(page(), other, now=NOW)

    assert result.text_similarity < 0.5
    assert (result.same_title, result.same_published_at, result.same_language) == (
        False,
        False,
        False,
    )


def test_diferenca_pequena_de_texto_continua_muito_similar():
    changed = page(TEXT.replace("quarta-feira", "quinta-feira"))

    assert compare_pages(page(), changed, now=NOW).text_similarity > 0.95


def test_razao_de_tamanho_mostra_corte_de_texto():
    short = page().replace(b"<p>" + TEXT.encode() + b"</p>", b"", 6)

    result = compare_pages(page(), short, now=NOW)

    assert result.length_ratio < 0.5


def test_um_lado_vazio_nao_levanta_e_zera_a_similaridade():
    result = compare_pages(page(), b"", now=NOW)

    assert result.text_similarity == 0.0
    assert result.length_ratio == 0.0
    assert compare_pages(b"", b"", now=NOW).text_similarity == 1.0
