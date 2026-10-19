import time
from datetime import UTC, datetime, timedelta

from buscador_midias.discovery.incremental import discover_site
from buscador_midias.discovery.sources import cascade_for
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import legacy_url_hash, url_hash
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.state.store import StateStore, UrlKind

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
UA = "BuscadorMidiasBot"
ROBOTS = b"Sitemap: https://exemplo.com/sitemap.xml\n"
INDEX_ROBOTS = b"Sitemap: https://exemplo.com/index.xml\n"


class FakeFetcher:
    def __init__(self, responses: dict[str, bytes | FetchResult]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        response = self.responses.get(url)
        if response is None:
            return FetchResult(url=url, status=404, content=b"", error=None)
        if isinstance(response, FetchResult):
            return response
        return FetchResult(url=url, status=200, content=response, error=None)


class FakeCorpus:
    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or set()
        self.calls: list[list[str]] = []

    def known_hashes(self, hashes: list[str]) -> set[str]:
        self.calls.append(list(hashes))
        return {h for h in hashes if h in self.known}


def make_site(url: str = "https://exemplo.com") -> Site:
    return Site.create(
        name="Exemplo",
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


Entry = tuple[str, str | None] | tuple[str, str | None, str | None, str | None]


def urlset(*entries: Entry) -> bytes:
    """Cada entrada: (loc, lastmod) ou (loc, lastmod, publication_date, title)."""
    body = ""
    for entry in entries:
        loc, lastmod = entry[0], entry[1]
        body += f"<url><loc>{loc}</loc>"
        if lastmod:
            body += f"<lastmod>{lastmod}</lastmod>"
        if len(entry) == 4:
            body += (
                f"<news:news><news:publication_date>{entry[2]}</news:publication_date>"
                f"<news:title>{entry[3]}</news:title></news:news>"
            )
        body += "</url>"
    return (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">'
        f"{body}</urlset>"
    ).encode()


def index(*children: tuple[str, str | None]) -> bytes:
    items = "".join(
        f"<sitemap><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</sitemap>"
        for loc, mod in children
    )
    return (
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
    ).encode()


def simple(*entries: tuple[str, str | None]) -> FakeFetcher:
    return FakeFetcher(
        {
            "https://exemplo.com/robots.txt": ROBOTS,
            "https://exemplo.com/sitemap.xml": urlset(*entries),
        }
    )


def sitemap_only(site: Site, fetcher: FakeFetcher, **kwargs):
    """Cascata só com o sitemap gratuito: o comportamento clássico da descoberta."""
    return cascade_for(
        site, free=fetcher, paid=None, user_agent=UA, known_blocked=False, use_feed=False, **kwargs
    )


def test_xml_acessivel_so_com_noticias_antigas_e_sucesso_sem_recentes():
    store = StateStore()
    run_id = store.start_run(NOW)
    fetcher = simple(('https://exemplo.com/antiga', '2026-10-01T12:00:00Z'))
    sources = sitemap_only(make_site(), fetcher, since=NOW - timedelta(days=2))
    result = discover_site(make_site(), sources, store, run_id=run_id, now=NOW, fresh_days=2)
    assert result.record.status == 'ok'
    assert result.record.new_urls == result.record.baseline_urls == 0
    assert 'sem notícias recentes' in result.record.detail
    store.close()


def test_publicacao_do_complemento_prevalece_sobre_lastmod_antigo():
    from tests.discovery.test_sources import strategy

    from buscador_midias.domain.strategy import Role, StrategyKind

    store = StateStore()
    run(store, simple(("https://exemplo.com/noticia-inicial", None)))
    site = make_site()
    target = "https://exemplo.com/noticia-inedita"
    free = FakeFetcher({
        "https://exemplo.com/robots.txt": b"",
        "https://exemplo.com/main.xml": urlset((target, "2020-01-01")),
        "https://exemplo.com/feed": rss((target, "Título", "Thu, 15 Oct 2026 12:00:00 +0000")),
    })
    sources = cascade_for(site, free=free, paid=None, user_agent=UA, known_blocked=False,
                          strategies=[
                              strategy("https://exemplo.com/main.xml", StrategyKind.SITEMAP,
                                       Role.PRIMARY),
                              strategy("https://exemplo.com/feed", StrategyKind.FEED,
                                       Role.COMPLEMENT),
                          ])
    result = discover_site(site, sources, store, run_id=store.start_run(NOW), now=NOW)
    assert result.record.new_urls == 1
    assert result.record.backlog_urls == 0
    assert result.new[0].publication_date == NOW
    store.close()


def test_checkpoint_dos_filhos_do_complemento_e_preservado_entre_rodadas():
    from tests.discovery.test_sources import strategy

    from buscador_midias.domain.strategy import Role, StrategyKind

    store = StateStore()
    site = make_site()
    child = "https://exemplo.com/child.xml"
    free = FakeFetcher({
        "https://exemplo.com/robots.txt": b"",
        "https://exemplo.com/main.xml": urlset(("https://exemplo.com/noticia-um", None)),
        "https://exemplo.com/index.xml": index((child, "2026-10-14")),
        child: urlset(("https://exemplo.com/noticia-dois", None)),
    })
    reports = [strategy("https://exemplo.com/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy("https://exemplo.com/index.xml", StrategyKind.SITEMAP, Role.COMPLEMENT)]
    for _ in range(2):
        sources = cascade_for(site, free=free, paid=None, user_agent=UA,
                              known_blocked=False, strategies=reports)
        result = discover_site(site, sources, store, run_id=store.start_run(NOW), now=NOW)
    assert free.calls.count(child) == 1
    assert store.sitemap_lastmod(child) == datetime(2026, 10, 14, tzinfo=UTC)
    assert result.record.sitemaps_skipped == 1
    assert result.new == ()
    store.close()


def run(store: StateStore, fetcher: FakeFetcher, *, site: Site | None = None, **kwargs):
    site = site or make_site()
    budget = (
        {"max_sitemap_fetches": kwargs.pop("max_sitemap_fetches")}
        if "max_sitemap_fetches" in kwargs
        else {}
    )
    run_id = store.start_run(NOW)
    return discover_site(
        site, sitemap_only(site, fetcher, **budget), store, run_id=run_id, now=NOW, **kwargs
    )


def test_primeira_rodada_do_site_e_linha_de_base_e_nao_traz_nada_novo():
    store = StateStore()

    result = run(
        store, simple(("https://exemplo.com/a", None), ("https://exemplo.com/materia-b", None))
    )

    assert result.record.status == "ok"
    assert result.record.baseline_urls == 2
    assert result.record.new_urls == 0
    assert result.record.seen_urls == 2
    assert result.new == ()
    assert store.site_has_baseline("exemplo.com")
    assert store.count_urls(kind=UrlKind.BASELINE) == 2


def test_segunda_rodada_identica_nao_acha_nada_novo():
    store = StateStore()
    fetcher = simple(("https://exemplo.com/a", None))
    run(store, fetcher)

    result = run(store, fetcher)

    assert result.new == ()
    assert result.record.baseline_urls == 0
    assert result.record.seen_urls == 1


def test_url_inedita_depois_da_linha_de_base_e_nova_uma_unica_vez():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    grown = simple(("https://exemplo.com/a", None), ("https://exemplo.com/materia-b", "2026-10-15"))

    second = run(store, grown)
    third = run(store, grown)

    assert [u.url for u in second.new] == ["https://exemplo.com/materia-b"]
    assert second.record.new_urls == 1
    assert third.new == ()
    assert store.count_urls(kind=UrlKind.NEW) == 1


def test_url_nova_sem_data_conta_como_nova():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))

    result = run(
        store, simple(("https://exemplo.com/a", None), ("https://exemplo.com/materia-b", None))
    )

    assert [u.url for u in result.new] == ["https://exemplo.com/materia-b"]


def test_url_inedita_mas_antiga_demais_e_backlog_e_nao_nova():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    fetcher = simple(
        ("https://exemplo.com/a", None),
        ("https://exemplo.com/materia-arquivo", "2026-01-01"),
        ("https://exemplo.com/materia-fresca", "2026-10-14"),
    )

    result = run(store, fetcher, fresh_days=7)

    assert [u.url for u in result.new] == ["https://exemplo.com/materia-fresca"]
    assert result.record.backlog_urls == 1
    assert store.count_urls(kind=UrlKind.BACKLOG) == 1


def test_a_janela_de_frescor_e_configuravel():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    fetcher = simple(
        ("https://exemplo.com/a", None), ("https://exemplo.com/materia-b", "2026-10-01")
    )

    result = run(store, fetcher, fresh_days=30)

    assert [u.url for u in result.new] == ["https://exemplo.com/materia-b"]


def test_nova_traz_titulo_e_data_de_publicacao_do_news_sitemap():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": ROBOTS,
            "https://exemplo.com/sitemap.xml": urlset(
                ("https://exemplo.com/a", None),
                (
                    "https://exemplo.com/materia-b",
                    None,
                    "2026-10-15T10:00:00Z",
                    "Operação prende diretor",
                ),
            ),
        }
    )

    result = run(store, fetcher)

    assert result.new[0].title == "Operação prende diretor"
    assert result.new[0].publication_date == datetime(2026, 10, 15, 10, tzinfo=UTC)


def test_url_ja_conhecida_pelo_corpus_nao_e_nova():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    known_url = "https://exemplo.com/ja-esta-no-corpus"
    corpus = FakeCorpus({url_hash(known_url)})
    fetcher = simple(
        ("https://exemplo.com/a", None), (known_url, None), ("https://exemplo.com/materia-b", None)
    )

    result = run(store, fetcher, corpus=corpus)

    assert [u.url for u in result.new] == ["https://exemplo.com/materia-b"]
    assert result.record.known_urls == 1
    assert store.count_urls(kind=UrlKind.KNOWN) == 1


def test_corpus_reconhece_linha_antiga_pelo_hash_legado():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    legacy_url = "https://www1.exemplo.com/politica/materia"
    legacy = legacy_url_hash(legacy_url)
    assert legacy is not None
    corpus = FakeCorpus({legacy})

    result = run(store, simple(("https://exemplo.com/a", None), (legacy_url, None)), corpus=corpus)

    assert result.new == ()
    assert result.record.known_urls == 1


def test_linha_de_base_nao_consulta_o_corpus():
    store = StateStore()
    corpus = FakeCorpus()

    run(store, simple(("https://exemplo.com/a", None)), corpus=corpus)

    assert corpus.calls == []


def test_so_candidatas_frescas_vao_ao_corpus_e_em_uma_unica_consulta():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    corpus = FakeCorpus()
    fetcher = simple(
        ("https://exemplo.com/a", None),
        ("https://exemplo.com/materia-velha", "2025-01-01"),
        ("https://exemplo.com/f1", "2026-10-15"),
        ("https://exemplo.com/f2", None),
    )

    run(store, fetcher, corpus=corpus)

    assert len(corpus.calls) == 1
    assert url_hash("https://exemplo.com/materia-velha") not in corpus.calls[0]
    assert {url_hash("https://exemplo.com/f1"), url_hash("https://exemplo.com/f2")} <= set(
        corpus.calls[0]
    )


def test_sem_candidatas_o_corpus_nao_e_consultado():
    store = StateStore()
    fetcher = simple(("https://exemplo.com/a", None))
    run(store, fetcher)
    corpus = FakeCorpus()

    run(store, fetcher, corpus=corpus)

    assert corpus.calls == []


def test_sitemap_filho_inalterado_e_pulado_e_o_alterado_e_relido():
    store = StateStore()
    responses: dict[str, bytes | FetchResult] = {
        "https://exemplo.com/robots.txt": INDEX_ROBOTS,
        "https://exemplo.com/index.xml": index(
            ("https://exemplo.com/estavel.xml", "2026-10-01T00:00:00Z"),
            ("https://exemplo.com/ativo.xml", "2026-10-15T08:00:00Z"),
        ),
        "https://exemplo.com/estavel.xml": urlset(("https://exemplo.com/e1", None)),
        "https://exemplo.com/ativo.xml": urlset(("https://exemplo.com/a1", None)),
    }
    run(store, FakeFetcher(dict(responses)))

    responses["https://exemplo.com/index.xml"] = index(
        ("https://exemplo.com/estavel.xml", "2026-10-01T00:00:00Z"),
        ("https://exemplo.com/ativo.xml", "2026-10-15T11:00:00Z"),
    )
    responses["https://exemplo.com/ativo.xml"] = urlset(
        ("https://exemplo.com/a1", None), ("https://exemplo.com/a2", "2026-10-15")
    )
    fetcher = FakeFetcher(dict(responses))

    result = run(store, fetcher)

    assert "https://exemplo.com/estavel.xml" not in fetcher.calls
    assert "https://exemplo.com/ativo.xml" in fetcher.calls
    assert result.record.sitemaps_skipped == 1
    assert [u.url for u in result.new] == ["https://exemplo.com/a2"]


def test_lastmod_do_filho_fica_registrado_para_a_proxima_rodada():
    store = StateStore()
    responses: dict[str, bytes | FetchResult] = {
        "https://exemplo.com/robots.txt": INDEX_ROBOTS,
        "https://exemplo.com/index.xml": index(
            ("https://exemplo.com/s.xml", "2026-10-14T00:00:00Z")
        ),
        "https://exemplo.com/s.xml": urlset(("https://exemplo.com/a", None)),
    }

    run(store, FakeFetcher(responses))

    assert store.sitemap_lastmod("https://exemplo.com/s.xml") == datetime(2026, 10, 14, tzinfo=UTC)


def test_falha_registra_o_status_e_nao_marca_linha_de_base():
    store = StateStore()
    blocked = FakeFetcher(
        {"https://exemplo.com/robots.txt": FetchResult("u", status=403, content=b"", error=None)}
    )

    failed = run(store, blocked)

    assert failed.record.status == "blocked"
    assert "403" in failed.record.detail
    assert failed.new == ()
    assert store.site_has_baseline("exemplo.com") is False

    later = run(store, simple(("https://exemplo.com/a", None)))
    assert later.record.baseline_urls == 1


def test_ignora_urls_de_outro_host_e_respeita_o_prefixo_de_secao():
    store = StateStore()
    fetcher = FakeFetcher(
        {
            "https://g1.globo.com/robots.txt": b"Sitemap: https://g1.globo.com/s.xml\n",
            "https://g1.globo.com/s.xml": urlset(
                ("https://g1.globo.com/globonews/a", None),
                ("https://g1.globo.com/politica/b", None),
                ("https://outro.com/c", None),
            ),
        }
    )

    result = run(store, fetcher, site=make_site("https://g1.globo.com/globonews"))

    assert result.record.seen_urls == 1
    assert result.record.baseline_urls == 1


def test_mesma_url_em_variantes_conta_uma_vez():
    store = StateStore()
    fetcher = simple(
        ("https://exemplo.com/a", None),
        ("http://www.exemplo.com/a/?utm_source=x", None),
    )

    result = run(store, fetcher)

    assert result.record.seen_urls == 1
    assert store.count_urls() == 1


def test_data_futura_ou_recente_nunca_vira_backlog():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    future = (NOW + timedelta(days=2)).strftime("%Y-%m-%d")

    result = run(
        store, simple(("https://exemplo.com/a", None), ("https://exemplo.com/materia-b", future))
    )

    assert [u.url for u in result.new] == ["https://exemplo.com/materia-b"]


def test_rodada_seguinte_nao_aprofunda_em_sitemaps_antigos_que_nunca_foram_lidos():
    children = tuple(
        (f"https://exemplo.com/s{n}.xml", f"2026-10-{15 - n:02d}T00:00:00Z") for n in range(1, 9)
    )
    pages: dict[str, bytes | FetchResult] = {
        "https://exemplo.com/robots.txt": INDEX_ROBOTS,
        "https://exemplo.com/index.xml": index(*children),
    }
    for n, (url, _) in enumerate(children):
        pages[url] = urlset((f"https://exemplo.com/artigo-{n}", None))
    store = StateStore()
    run(store, FakeFetcher(dict(pages)))  # linha de base: índice + os 5 filhos mais recentes

    second_fetcher = FakeFetcher(dict(pages))
    result = run(store, second_fetcher)

    children_read = [c for c in second_fetcher.calls if "/s" in c and c.endswith(".xml")]
    assert children_read == []
    assert result.record.sitemaps_skipped == 5
    assert result.new == ()
    assert result.record.backlog_urls == 0


def test_milhares_de_urls_inéditas_sao_processadas_em_tempo_linear():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    archive = tuple((f"https://exemplo.com/arquivo-{n}", "2024-01-01") for n in range(12_000))

    started = time.perf_counter()
    result = run(store, simple(("https://exemplo.com/a", None), *archive))
    elapsed = time.perf_counter() - started

    assert result.record.backlog_urls == 12_000
    assert elapsed < 3.0


def test_filho_que_falha_fica_registrado_mesmo_com_o_site_ok():
    fetcher = FakeFetcher(
        {
            "https://exemplo.com/robots.txt": INDEX_ROBOTS,
            "https://exemplo.com/index.xml": index(
                ("https://exemplo.com/a.xml", "2026-10-15T00:00:00Z"),
                ("https://exemplo.com/b.xml", "2026-10-14T00:00:00Z"),
            ),
            "https://exemplo.com/a.xml": urlset(("https://exemplo.com/materia-x", None)),
            "https://exemplo.com/b.xml": FetchResult(
                url="https://exemplo.com/b.xml", status=None, content=b"", error="timeout"
            ),
        }
    )
    store = StateStore()

    result = run(store, fetcher)

    assert result.record.status == "ok"
    assert result.record.sitemaps_failed == 1
    assert "b.xml" in result.record.detail


def test_rodada_sem_falhas_nao_registra_falha_nem_detalhe():
    result = run(StateStore(), simple(("https://exemplo.com/a", None)))

    assert result.record.sitemaps_failed == 0
    assert result.record.detail == ""


def _after_baseline(store: StateStore, *new_paths: str) -> FakeFetcher:
    """Linha de base com uma matéria e, na rodada seguinte, o sitemap ganha ``new_paths``."""
    run(store, simple(("https://exemplo.com/materia-base-1", None)))
    return simple(
        ("https://exemplo.com/materia-base-1", None),
        *((f"https://exemplo.com/{path}", None) for path in new_paths),
    )


def test_pagina_de_listagem_inedita_e_filtrada_e_nao_vira_nova():
    store = StateStore()
    fetcher = _after_baseline(store, "tag/eleicoes", "politica/governo-anuncia-medida")

    result = run(store, fetcher)

    assert [u.url for u in result.new] == ["https://exemplo.com/politica/governo-anuncia-medida"]
    assert result.record.new_urls == 1
    assert result.record.filtered_urls == 1
    assert store.count_urls(kind=UrlKind.FILTERED) == 1
    assert store.filtered_by_reason() == {"listing": 1}


def test_linha_de_base_nao_filtra_nada():
    store = StateStore()

    result = run(store, simple(("https://exemplo.com/tag/eleicoes", None)))

    assert result.record.baseline_urls == 1
    assert result.record.filtered_urls == 0


def test_corpus_nao_e_consultado_para_o_que_o_l0_ja_descartou():
    store = StateStore()
    fetcher = _after_baseline(store, "tag/eleicoes", "autor/maria", "secao")
    corpus = FakeCorpus()

    run(store, fetcher, corpus=corpus)

    assert corpus.calls == []


def test_tema_nao_e_descartado_por_padrao():
    store = StateStore()
    fetcher = _after_baseline(store, "esporte/futebol/clube-vence-o-classico")

    default = run(store, fetcher)

    assert len(default.new) == 1


def test_tema_so_e_descartado_quando_pedido():
    store = StateStore()
    fetcher = _after_baseline(store, "esporte/futebol/clube-vence-o-classico")

    asked = run(store, fetcher, off_topic_segments=frozenset({"esporte"}))

    assert asked.new == ()
    assert asked.record.filtered_urls == 1
    assert store.filtered_by_reason() == {"off_topic": 1}


def test_url_filtrada_nao_volta_a_ser_avaliada_na_rodada_seguinte():
    store = StateStore()
    fetcher = _after_baseline(store, "tag/eleicoes")
    run(store, fetcher)

    again = run(store, fetcher)

    assert again.record.filtered_urls == 0
    assert again.record.new_urls == 0


# --- cascata: sitemap -> feed -> sitemap pago ---

RSS_HOME = (
    b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed/"></head></html>'
)


def rss(*items: tuple[str, str, str]) -> bytes:
    """Cada item: (link, título, data RFC 822)."""
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate></item>"
        for link, title, date in items
    )
    return f'<rss version="2.0"><channel>{body}</channel></rss>'.encode()


BLOCKED_ROBOTS = {
    "https://exemplo.com/robots.txt": FetchResult("u", status=403, content=b"", error=None)
}


def cascade_run(store: StateStore, free: FakeFetcher, paid=None, *, known_blocked=False):
    site = make_site()
    sources = cascade_for(
        site, free=free, paid=paid, user_agent=UA, known_blocked=known_blocked, use_feed=True
    )
    return discover_site(site, sources, store, run_id=store.start_run(NOW), now=NOW)


def test_o_registro_do_site_diz_de_que_fonte_vieram_as_urls():
    store = StateStore()

    result = run(store, simple(("https://exemplo.com/a", None)))

    assert result.record.source == "sitemap"


def test_sitemap_bloqueado_cai_para_o_feed_e_as_novas_vem_dele():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))  # linha de base pelo sitemap
    free = FakeFetcher(
        {
            "https://exemplo.com/sitemap.xml": FetchResult("u", 403, b"", None),
            "https://exemplo.com": RSS_HOME,
            "https://exemplo.com/feed/": rss(
                (
                    "https://exemplo.com/politica/pf-prende-empresario",
                    "PF prende empresário",
                    "Thu, 15 Oct 2026 10:00:00 +0000",
                )
            ),
        }
    )

    result = cascade_run(store, free)

    assert result.record.status == "ok"
    assert result.record.source == "feed"
    [found] = result.new
    assert found.title == "PF prende empresário"
    assert found.publication_date == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert store.sitemap_lastmod("feed:exemplo.com") is None


def test_urls_do_feed_antigas_demais_viram_backlog_como_as_do_sitemap():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    feed = FakeFetcher(
        {
            "https://exemplo.com": RSS_HOME,
            "https://exemplo.com/feed/": rss(
                ("https://exemplo.com/velha-materia", "Velha", "Mon, 01 Jun 2026 10:00:00 +0000"),
                ("https://exemplo.com/fresca-materia", "Fresca", "Thu, 15 Oct 2026 10:00:00 +0000"),
            ),
            "https://exemplo.com/sitemap.xml": FetchResult("u", 404, b"", None),
        }
    )

    result = cascade_run(store, feed)

    assert [u.url for u in result.new] == ["https://exemplo.com/fresca-materia"]
    assert result.record.backlog_urls == 1


def test_primeira_leitura_pelo_feed_tambem_e_linha_de_base():
    store = StateStore()
    feed = FakeFetcher(
        {
            "https://exemplo.com": RSS_HOME,
            "https://exemplo.com/feed/": rss(
                ("https://exemplo.com/materia-um", "Um", "Thu, 15 Oct 2026 10:00:00 +0000")
            ),
        }
    )

    result = cascade_run(store, feed)

    assert result.record.source == "feed"
    assert result.record.baseline_urls == 1
    assert result.new == ()


def test_sem_nenhuma_fonte_o_registro_vem_sem_fonte_e_junta_os_motivos():
    store = StateStore()
    free = FakeFetcher(dict(BLOCKED_ROBOTS))

    result = cascade_run(store, free)

    assert result.record.status == "blocked"
    assert result.record.source == ""
    assert result.record.detail.startswith("sitemap: robots.txt: HTTP 403; feed:")
    assert store.site_has_baseline("exemplo.com") is False


def test_sitemap_pago_resgata_o_bloqueio_e_marca_o_host_como_dependente():
    store = StateStore()
    free = FakeFetcher(dict(BLOCKED_ROBOTS))
    paid = simple(("https://exemplo.com/a", None))

    result = cascade_run(store, free, paid)

    assert result.record.source == "sitemap_bd"
    assert result.record.baseline_urls == 1
    assert store.fallback_hosts() == frozenset({"exemplo.com"})


def test_host_conhecido_como_bloqueado_tenta_o_feed_antes_de_pagar():
    store = StateStore()
    run(store, simple(("https://exemplo.com/a", None)))
    free = FakeFetcher(
        {
            "https://exemplo.com": RSS_HOME,
            "https://exemplo.com/feed/": rss(
                ("https://exemplo.com/materia-nova", "Nova", "Thu, 15 Oct 2026 10:00:00 +0000")
            ),
        }
    )
    paid = simple(("https://exemplo.com/a", None))

    result = cascade_run(store, free, paid, known_blocked=True)

    assert result.record.source == "feed"
    assert paid.calls == []
    assert "https://exemplo.com/sitemap.xml" not in free.calls
