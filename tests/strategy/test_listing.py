"""Varredura de listagens: da home às páginas de seção, atrás de links de matéria."""

import pytest
from tests.strategy.helpers import HOME, NOW, UA, FakeFetcher, make_site, robots, status

from buscador_midias.discovery.robots import RobotsPolicy
from buscador_midias.domain.strategy import CandidateOrigin, StrategyKind, Verdict
from buscador_midias.strategy.listing import (
    extract_links,
    is_article_url,
    is_section_url,
    scan_listing,
)
from buscador_midias.strategy.scoring import ScoringPolicy


def page(*hrefs: str) -> bytes:
    return (
        "<html><body>" + "".join(f'<a href="{h}">x</a>' for h in hrefs) + "</body></html>"
    ).encode()


def art(n: int, section: str = "") -> str:
    prefix = f"/{section}" if section else ""
    return f"{prefix}/governo-anuncia-medida-numero-{n}-para-o-setor"


# --- extração ---


def test_extrai_links_do_proprio_site_resolvidos_sem_ancora_ativos_e_sem_repeticao():
    html = page(
        "/politica",
        "/politica#topo",
        f"{HOME}/economia/",
        "https://outro.com/x-y-z-w",
        "/img/logo.png",
        "mailto:a@b.com",
        "javascript:void(0)",
        "relativo/pagina",
    )

    links = extract_links(html, f"{HOME}/", make_site())

    assert links == [f"{HOME}/politica", f"{HOME}/economia", f"{HOME}/relativo/pagina"]


def test_links_fora_do_prefixo_de_uma_fonte_de_secao_nao_contam():
    site = make_site(f"{HOME}/ac/acre")

    links = extract_links(page("/ac/acre/noticia-a", "/sp/outra"), f"{HOME}/ac/acre", site)

    assert links == [f"{HOME}/ac/acre/noticia-a"]


# --- o que é matéria e o que é seção ---


@pytest.mark.parametrize(
    "url",
    [
        f"{HOME}/hybrid-repression-coordinated-attacks-on-rwanda-press-freedom",
        f"{HOME}/2026/10/grace-fu-preventive-measures-notice",
        f"{HOME}/articles/-/88431",
        f"{HOME}/story/2635052/irans-midday-meal-takes-top-prize",
        f"{HOME}/politica/noticia/2026/10/15/governo-anuncia-medida.ghtml",
    ],
)
def test_url_com_cara_de_materia(url):
    assert is_article_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"{HOME}/politica",
        f"{HOME}/ac/acre",
        f"{HOME}/category/news",
        f"{HOME}/contact-us",
        f"{HOME}/my-profile?view=reset",
        f"{HOME}/tag/economia-global-mercado-juros-inflacao",
        f"{HOME}/wp-content/uploads/2026/10/foto.jpg",
        HOME + "/",
    ],
)
def test_url_sem_cara_de_materia(url):
    assert not is_article_url(url)


@pytest.mark.parametrize(
    "url",
    [f"{HOME}/politica", f"{HOME}/ac/acre", f"{HOME}/world/africa", f"{HOME}/category/news"],
)
def test_url_com_cara_de_secao(url):
    assert is_section_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"{HOME}/login",
        f"{HOME}/about",
        f"{HOME}/privacy-policy",
        f"{HOME}/2026",
        f"{HOME}/a/b/c/d",
        f"{HOME}/governo-anuncia-medida-numero-1-para-o-setor",
        f"{HOME}/my-profile?view=reset",
        HOME + "/",
    ],
)
def test_url_que_nao_e_secao(url):
    assert not is_section_url(url)


# --- varredura ---


def world(**pages: bytes) -> FakeFetcher:
    return FakeFetcher(
        {f"{HOME}{path}": body for path, body in pages.items()} | {f"{HOME}/robots.txt": robots()}
    )


def scan(fetcher, *, max_pages=10, robots_policy=None, site=None):
    return scan_listing(
        site or make_site(),
        fetcher,
        robots=robots_policy or RobotsPolicy.allow_all(),
        user_agent=UA,
        max_pages=max_pages,
    )


def test_a_varredura_junta_as_materias_da_home_e_das_secoes_e_conta_as_paginas():
    fetcher = FakeFetcher(
        {
            HOME: page("/politica", "/economia", art(1)),
            f"{HOME}/politica": page(art(2, "politica"), art(3, "politica")),
            f"{HOME}/economia": page(art(4, "economia")),
        }
    )

    result = scan(fetcher)

    assert set(result.articles) == {
        f"{HOME}{art(1)}",
        f"{HOME}{art(2, 'politica')}",
        f"{HOME}{art(3, 'politica')}",
        f"{HOME}{art(4, 'economia')}",
    }
    assert result.pages == 3 and result.requests == 3
    assert set(result.sections) == {f"{HOME}/politica", f"{HOME}/economia"}


def test_secao_achada_numa_pagina_de_secao_tambem_e_visitada():
    """A URL de uma seção ajuda a achar as outras (sub-seções que a home não mostra)."""
    fetcher = FakeFetcher(
        {
            HOME: page("/mundo"),
            f"{HOME}/mundo": page("/mundo/africa", art(1, "mundo")),
            f"{HOME}/mundo/africa": page(art(2, "mundo/africa")),
        }
    )

    result = scan(fetcher)

    assert f"{HOME}{art(2, 'mundo/africa')}" in result.articles
    assert f"{HOME}/mundo/africa" in result.sections


def test_o_teto_de_paginas_limita_as_requisicoes():
    sections = [f"/secao{chr(97 + n)}" for n in range(10)]
    pages = {"": page(*sections)} | {s: page(art(n, s.strip("/"))) for n, s in enumerate(sections)}
    fetcher = FakeFetcher({f"{HOME}{k}" if k else HOME: v for k, v in pages.items()})

    result = scan(fetcher, max_pages=4)

    assert result.pages == 4 and len(fetcher.calls) == 4


def test_pagina_de_secao_que_falha_ou_o_robots_proibe_nao_derruba_a_varredura():
    fetcher = FakeFetcher(
        {
            HOME: page("/politica", "/privado", "/economia", art(1)),
            f"{HOME}/politica": status(f"{HOME}/politica", 500),
            f"{HOME}/economia": page(art(2, "economia")),
        }
    )
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /privado\n")

    result = scan(fetcher, robots_policy=policy)

    assert f"{HOME}{art(2, 'economia')}" in result.articles
    assert f"{HOME}/politica" in fetcher.calls  # tentou, falhou e seguiu
    assert f"{HOME}/privado" not in fetcher.calls  # o robots.txt proíbe


def test_home_que_nao_abre_nao_tem_varredura():
    fetcher = FakeFetcher({HOME: status(HOME, 403)})

    result = scan(fetcher)

    assert result.articles == () and result.failure is not None


# --- como candidata ---


def evaluate(fetcher, *, scoring=None):
    from buscador_midias.strategy.listing import evaluate_listing

    return evaluate_listing(
        make_site(),
        fetcher,
        robots=RobotsPolicy.allow_all(),
        user_agent=UA,
        now=NOW,
        scoring=scoring,
    )


def busy_home() -> FakeFetcher:
    return FakeFetcher(
        {
            HOME: page("/politica", "/economia", *(art(n) for n in range(6))),
            f"{HOME}/politica": page(*(art(n, "politica") for n in range(10, 14))),
            f"{HOME}/economia": page(*(art(n, "economia") for n in range(20, 24))),
        }
    )


def test_listagem_com_materias_de_sobra_vira_candidata_sem_datas_e_com_nota_baixa():
    report = evaluate(busy_home())

    assert report.kind is StrategyKind.LISTING and report.verdict is Verdict.UNDATED
    assert report.origin is CandidateOrigin.HOME_LINK and report.url == HOME
    assert report.measure is not None and report.measure.items == 14
    assert (
        report.measure.dated == 0 and report.measure.sections == 3
    )  # (outros), politica, economia
    assert 0 < report.score < 1
    assert report.requests == 3 and "3 páginas" in report.detail


def test_a_listagem_pontua_menos_que_qualquer_outro_tipo_sem_datas_ou_lento():
    from buscador_midias.strategy.scoring import score

    report = evaluate(busy_home())
    assert report.measure is not None

    sitemap_undated = score(
        report.measure.__class__(StrategyKind.SITEMAP, 5, 0, 0, 5, None, 10), Verdict.UNDATED
    )
    assert report.score < sitemap_undated


def test_poucas_materias_nao_sao_uma_estrategia():
    fetcher = FakeFetcher(
        {HOME: page("/politica", art(1), art(2)), f"{HOME}/politica": page(art(3))}
    )

    report = evaluate(fetcher, scoring=ScoringPolicy())

    assert report.verdict is Verdict.EMPTY and report.score == 0


def test_home_bloqueada_leva_o_motivo():
    report = evaluate(FakeFetcher({HOME: status(HOME, 403)}))

    assert report.verdict is Verdict.BLOCKED and report.score == 0
