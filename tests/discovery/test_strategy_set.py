from dataclasses import replace

from tests.discovery.test_sources import HOME, UA, FakeFetcher, make_site, strategy, urlset
from tests.strategy.helpers import rss

from buscador_midias.discovery.sources import SourceKind, cascade_for, run_cascade
from buscador_midias.domain.strategy import Role, StrategyKind


def collect(fetcher, reports, **kwargs):
    return run_cascade(make_site(), cascade_for(
        make_site(), free=fetcher, paid=None, user_agent=UA, known_blocked=False,
        strategies=reports, **kwargs,
    ))


def test_principal_e_complemento_sao_unidos_sem_ler_reserva_ou_fallback():
    reports = [strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy(HOME + "/backup", StrategyKind.FEED, Role.BACKUP),
               strategy(HOME + "/feed", StrategyKind.FEED, Role.COMPLEMENT)]
    fetcher = FakeFetcher({HOME + "/robots.txt": b"", HOME + "/main.xml": urlset(
        HOME + "/materia-um", HOME + "/materia-dois/"),
        HOME + "/feed": rss(("materia-dois", 1), ("materia-tres", 1))})
    result = collect(fetcher, reports)
    assert result.chosen is not None
    assert result.chosen.kind is SourceKind.COMBINED
    assert len(result.chosen.urls) == 3
    shared = next(u for u in result.chosen.urls if u.url.rstrip("/").endswith("materia-dois"))
    assert shared.publication_date is not None and shared.title
    assert HOME + "/backup" not in fetcher.calls
    assert HOME + "/sitemap.xml" not in fetcher.calls


def test_falha_de_complemento_preserva_principal_e_indica_leitura_parcial():
    reports = [strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy(HOME + "/ausente", StrategyKind.FEED, Role.COMPLEMENT)]
    result = collect(FakeFetcher({HOME + "/main.xml": urlset(HOME + "/noticia")}), reports)
    assert result.chosen is not None and len(result.chosen.urls) == 1
    assert result.chosen.failed > 0
    assert result.chosen.failure_detail


def test_complemento_pode_servir_quando_principal_e_reserva_falham():
    reports = [strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy(HOME + "/backup", StrategyKind.FEED, Role.BACKUP),
               strategy(HOME + "/feed", StrategyKind.FEED, Role.COMPLEMENT)]
    result = collect(FakeFetcher({HOME + "/feed": rss(("materia-recente", 1))}), reports)
    assert result.chosen is not None
    assert [u.url for u in result.chosen.urls] == [HOME + "/materia-recente"]


def test_orcamento_total_preserva_urls_do_principal_e_informa_complemento_pendente():
    reports = [strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy(HOME + "/feed", StrategyKind.FEED, Role.COMPLEMENT)]
    fetcher = FakeFetcher({HOME + "/robots.txt": b"", HOME + "/main.xml": urlset(
        HOME + "/noticia"), HOME + "/feed": rss(("materia-recente", 1))})
    result = collect(fetcher, reports, max_source_requests=3)
    assert len(fetcher.calls) <= 3
    assert result.chosen is not None and len(result.chosen.urls) == 1
    assert result.chosen.failed > 0
    assert "limite" in result.chosen.failure_detail


def test_robots_de_subdominio_e_exclusoes_valem_nos_filhos_do_complemento():
    other = "https://blog.exemplo.com/feed"
    reports = [strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy(other, StrategyKind.FEED, Role.COMPLEMENT),
               strategy(HOME + "/excluido", StrategyKind.FEED, Role.COMPLEMENT)]
    fetcher = FakeFetcher({HOME + "/robots.txt": b"", HOME + "/main.xml": urlset(
        HOME + "/noticia"), "https://blog.exemplo.com/robots.txt": b"User-agent: *\nDisallow: /\n"})
    result = collect(fetcher, reports, exclude=(HOME + "/excluido",))
    assert result.chosen is not None
    assert other not in fetcher.calls and HOME + "/excluido" not in fetcher.calls


def test_enderecos_repetidos_e_de_fora_do_catalogo_nao_sao_lidos():
    main = strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY)
    complement = strategy(HOME + "/feed", StrategyKind.FEED, Role.COMPLEMENT)
    fetcher = FakeFetcher({HOME + "/robots.txt": b"", HOME + "/main.xml": urlset(
        HOME + "/noticia"), HOME + "/feed": rss(("materia-recente", 1))})
    result = collect(fetcher, [main, complement, complement, replace(
        complement, url="https://outro.com/feed")])
    assert result.chosen is not None
    assert fetcher.calls.count(HOME + "/feed") == 1
    assert not any(u.startswith("https://outro.com") for u in fetcher.calls)


def test_limite_de_complementos_e_no_feed_sao_respeitados():
    reports = [strategy(HOME + "/main.xml", StrategyKind.SITEMAP, Role.PRIMARY),
               strategy(HOME + "/feed", StrategyKind.FEED, Role.COMPLEMENT),
               strategy(HOME + "/mais.xml", StrategyKind.SITEMAP, Role.COMPLEMENT)]
    f = FakeFetcher({HOME + "/main.xml": urlset(HOME + "/noticia"),
                     HOME + "/feed": rss(("noticia-feed", 1)),
                     HOME + "/mais.xml": urlset(HOME + "/noticia-extra")})
    result = collect(f, reports, max_complements=1)
    assert result.chosen is not None and result.chosen.failed == 1
    assert HOME + "/mais.xml" not in f.calls
    f.calls.clear()
    result = collect(f, reports, use_feed=False)
    assert result.chosen is not None and len(result.chosen.urls) == 2
    assert HOME + "/feed" not in f.calls


def test_bloqueio_so_do_feed_nao_dispara_sitemap_pago():
    from buscador_midias.net.fetcher import FetchResult

    f = FakeFetcher({HOME + "/robots.txt": b"", HOME + "/feed": FetchResult(
        HOME + "/feed", 403, b"", None)})
    paid = FakeFetcher({})
    sources = cascade_for(make_site(), free=f, paid=paid, known_blocked=False, user_agent=UA,
                          strategies=[strategy(HOME + "/main.xml", StrategyKind.SITEMAP,
                                               Role.PRIMARY),
                                      strategy(HOME + "/feed", StrategyKind.FEED,
                                               Role.COMPLEMENT)])
    result = run_cascade(make_site(), sources)
    assert result.chosen is None
    assert paid.calls == []
