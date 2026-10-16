from datetime import UTC, datetime

from buscador_midias.discovery.explore import ProbeStatus
from buscador_midias.discovery.sources import (
    FeedSource,
    SitemapSource,
    SourceFind,
    SourceKind,
    cascade_for,
    run_cascade,
)
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import FetchResult

UA = "BuscadorMidiasBot"
HOME = "https://exemplo.com"


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


class ScriptedSource:
    """Fonte de mentirinha: devolve o que foi combinado e anota que foi chamada."""

    def __init__(self, kind: SourceKind, status: ProbeStatus, *, urls: tuple = (), force=False):
        self.kind = kind
        self._status = status
        self._urls = urls
        self._force = force
        self.called = False

    def applies(self, previous):
        if self.kind is not SourceKind.SITEMAP_PAID or self._force:
            return True
        return any(
            p.kind is SourceKind.SITEMAP and p.status is ProbeStatus.BLOCKED for p in previous
        )

    def find(self, site, *, skip_child=None) -> SourceFind:
        self.called = True
        detail = "" if self._status is ProbeStatus.OK else f"{self.kind.value} falhou"
        return SourceFind(self.kind, self._status, detail, self._urls)


def make_site(url: str = HOME) -> Site:
    return Site.create(
        name="Exemplo",
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def urlset(*locs: str) -> bytes:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'.encode()


def test_fonte_sitemap_devolve_as_urls_da_fonte_sem_repetir():
    fetcher = FakeFetcher(
        {
            f"{HOME}/robots.txt": f"Sitemap: {HOME}/sitemap.xml\n".encode(),
            f"{HOME}/sitemap.xml": urlset(
                f"{HOME}/materia-um", f"{HOME}/materia-um/", "https://outro.com/de-fora"
            ),
        }
    )

    find = SitemapSource(SourceKind.SITEMAP, fetcher, user_agent=UA).find(make_site())

    assert find.kind is SourceKind.SITEMAP
    assert find.status is ProbeStatus.OK
    assert [u.url for u in find.urls] == [f"{HOME}/materia-um"]
    assert find.urls[0].source_sitemap == f"{HOME}/sitemap.xml"
    assert [s.url for s in find.sitemaps] == [f"{HOME}/sitemap.xml"]
    assert find.fetched == 1


def test_fonte_sitemap_com_falha_traz_o_motivo_e_nenhuma_url():
    fetcher = FakeFetcher({f"{HOME}/robots.txt": FetchResult(f"{HOME}/robots.txt", 403, b"", None)})

    find = SitemapSource(SourceKind.SITEMAP, fetcher, user_agent=UA).find(make_site())

    assert find.status is ProbeStatus.BLOCKED
    assert find.urls == ()
    assert "403" in find.detail


def test_fonte_de_feed_vira_urls_com_titulo_e_data_de_publicacao():
    xml = (
        b'<rss version="2.0"><channel><item><title>PF prende</title>'
        b"<link>https://exemplo.com/politica/pf-prende</link>"
        b"<pubDate>Thu, 15 Oct 2026 10:00:00 +0000</pubDate></item></channel></rss>"
    )
    fetcher = FakeFetcher({HOME: b"<html></html>", f"{HOME}/feed/": xml})

    find = FeedSource(fetcher, user_agent=UA).find(make_site())

    assert find.kind is SourceKind.FEED
    assert find.status is ProbeStatus.OK
    [url] = find.urls
    assert url.url == f"{HOME}/politica/pf-prende"
    assert url.title == "PF prende"
    assert url.publication_date == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert url.lastmod is None
    assert url.source_sitemap == "feed:exemplo.com"
    assert find.sitemaps == ()


def test_cascata_para_na_primeira_fonte_que_serve():
    first = ScriptedSource(SourceKind.SITEMAP, ProbeStatus.OK, urls=("u",))
    second = ScriptedSource(SourceKind.FEED, ProbeStatus.OK)

    result = run_cascade(make_site(), [first, second])

    assert result.chosen is not None and result.chosen.kind is SourceKind.SITEMAP
    assert second.called is False
    assert [a.kind for a in result.attempts] == [SourceKind.SITEMAP]


def test_cascata_passa_para_o_feed_quando_o_sitemap_nao_serve():
    sitemap = ScriptedSource(SourceKind.SITEMAP, ProbeStatus.NO_SITEMAP)
    feed = ScriptedSource(SourceKind.FEED, ProbeStatus.OK, urls=("u",))
    paid = ScriptedSource(SourceKind.SITEMAP_PAID, ProbeStatus.OK)

    result = run_cascade(make_site(), [sitemap, feed, paid])

    assert result.chosen is not None and result.chosen.kind is SourceKind.FEED
    assert paid.called is False


def test_pago_so_entra_depois_de_um_bloqueio_do_sitemap_gratuito():
    sitemap = ScriptedSource(SourceKind.SITEMAP, ProbeStatus.BLOCKED)
    feed = ScriptedSource(SourceKind.FEED, ProbeStatus.NO_SITEMAP)
    paid = ScriptedSource(SourceKind.SITEMAP_PAID, ProbeStatus.OK, urls=("u",))

    result = run_cascade(make_site(), [sitemap, feed, paid])

    assert result.chosen is not None and result.chosen.kind is SourceKind.SITEMAP_PAID
    assert [a.kind for a in result.attempts] == [
        SourceKind.SITEMAP,
        SourceKind.FEED,
        SourceKind.SITEMAP_PAID,
    ]


def test_pago_nao_entra_quando_o_sitemap_apenas_nao_existe_ou_deu_erro():
    for status in (ProbeStatus.NO_SITEMAP, ProbeStatus.ERROR, ProbeStatus.ROBOTS_DISALLOWED):
        paid = ScriptedSource(SourceKind.SITEMAP_PAID, ProbeStatus.OK)

        result = run_cascade(
            make_site(),
            [
                ScriptedSource(SourceKind.SITEMAP, status),
                ScriptedSource(SourceKind.FEED, ProbeStatus.NO_SITEMAP),
                paid,
            ],
        )

        assert result.chosen is None
        assert paid.called is False, status


def test_pago_forcado_roda_mesmo_sem_tentativa_gratuita_de_sitemap():
    feed = ScriptedSource(SourceKind.FEED, ProbeStatus.NO_SITEMAP)
    paid = ScriptedSource(SourceKind.SITEMAP_PAID, ProbeStatus.OK, urls=("u",), force=True)

    result = run_cascade(make_site(), [feed, paid])

    assert result.chosen is not None and result.chosen.kind is SourceKind.SITEMAP_PAID


def test_falha_total_usa_o_pior_status_e_junta_os_motivos():
    result = run_cascade(
        make_site(),
        [
            ScriptedSource(SourceKind.SITEMAP, ProbeStatus.BLOCKED),
            ScriptedSource(SourceKind.FEED, ProbeStatus.NO_SITEMAP),
        ],
    )

    assert result.chosen is None
    assert result.status is ProbeStatus.BLOCKED
    assert result.detail == "sitemap: sitemap falhou; feed: feed falhou"


def test_com_uma_unica_tentativa_o_detalhe_vem_sem_prefixo():
    result = run_cascade(make_site(), [ScriptedSource(SourceKind.SITEMAP, ProbeStatus.BLOCKED)])

    assert result.detail == "sitemap falhou"


def test_erro_vence_a_ausencia_na_falha_total():
    result = run_cascade(
        make_site(),
        [
            ScriptedSource(SourceKind.SITEMAP, ProbeStatus.NO_SITEMAP),
            ScriptedSource(SourceKind.FEED, ProbeStatus.ERROR),
        ],
    )

    assert result.status is ProbeStatus.ERROR


def test_sucesso_nao_traz_detalhe_de_falha():
    result = run_cascade(make_site(), [ScriptedSource(SourceKind.SITEMAP, ProbeStatus.OK)])

    assert result.status is ProbeStatus.OK
    assert result.detail == ""


def kinds(sources) -> list[SourceKind]:
    return [s.kind for s in sources]


def build(**kwargs):
    base = {"free": FakeFetcher({}), "paid": None, "user_agent": UA, "known_blocked": False}
    base.update(kwargs)
    return cascade_for(make_site(), **base)


def test_sem_bright_data_a_cascata_e_sitemap_e_feed():
    assert kinds(build()) == [SourceKind.SITEMAP, SourceKind.FEED]


def test_com_bright_data_a_cascata_e_sitemap_feed_e_sitemap_pago():
    assert kinds(build(paid=FakeFetcher({}))) == [
        SourceKind.SITEMAP,
        SourceKind.FEED,
        SourceKind.SITEMAP_PAID,
    ]


def test_host_ja_conhecido_como_bloqueado_pula_o_sitemap_gratuito_mas_tenta_o_feed():
    sources = build(paid=FakeFetcher({}), known_blocked=True)

    assert kinds(sources) == [SourceKind.FEED, SourceKind.SITEMAP_PAID]
    assert sources[-1].applies([]) is True


def test_host_bloqueado_sem_bright_data_continua_tentando_o_sitemap_gratuito():
    assert kinds(build(known_blocked=True)) == [SourceKind.SITEMAP, SourceKind.FEED]


def test_feed_pode_ser_desligado():
    assert kinds(build(paid=FakeFetcher({}), use_feed=False)) == [
        SourceKind.SITEMAP,
        SourceKind.SITEMAP_PAID,
    ]
