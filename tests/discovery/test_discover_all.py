from datetime import UTC, datetime, timedelta

from buscador_midias.discovery.incremental import discover_all
from buscador_midias.discovery.sources import cascade_for
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import FetchResult
from buscador_midias.state.store import StateStore, UrlKind

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
UA = "BuscadorMidiasBot"


def make_site(host: str) -> Site:
    return Site.create(
        name=host,
        url=f"https://{host}",
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def sitemap(*locs: str) -> bytes:
    urls = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()


class World:
    """Dois sites com sitemap e um que explode; o conteúdo pode crescer entre rodadas."""

    def __init__(self) -> None:
        self.pages: dict[str, list[str]] = {
            "a.com": ["https://a.com/1"],
            "b.com": ["https://b.com/1", "https://b.com/2"],
        }

    def get(self, url: str) -> FetchResult:
        if "quebra.com" in url:
            raise RuntimeError("bug inesperado")
        host = url.split("/")[2]
        if url.endswith("/robots.txt"):
            body = f"Sitemap: https://{host}/sitemap.xml\n".encode()
        elif url.endswith("/sitemap.xml") and host in self.pages:
            body = sitemap(*self.pages[host])
        else:
            return FetchResult(url=url, status=404, content=b"", error=None)
        return FetchResult(url=url, status=200, content=body, error=None)


def sources_for(fetcher):
    """Cascata só com o sitemap gratuito, para cada site."""
    return lambda site: cascade_for(
        site, free=fetcher, paid=None, user_agent=UA, known_blocked=False, use_feed=False
    )


def clock_from(*instants: datetime):
    pending = list(instants)
    return lambda: pending.pop(0) if len(pending) > 1 else pending[0]


def test_rodada_cria_run_grava_resultado_por_site_e_fecha_a_run():
    store = StateStore()
    sites = [make_site("a.com"), make_site("b.com")]

    result = discover_all(
        sites,
        sources_for(World()),
        store,
        workers=2,
        clock=clock_from(T0, T0 + timedelta(minutes=3)),
    )

    assert result.run_id == 1
    assert [r.site_key for r in result.records] == ["a.com", "b.com"]
    assert [r.baseline_urls for r in result.records] == [1, 2]
    assert result.new_urls == ()
    run = store.list_runs()[0]
    assert run.started_at == T0
    assert run.finished_at == T0 + timedelta(minutes=3)
    assert [r.site_key for r in store.site_runs(1)] == ["a.com", "b.com"]


def test_segunda_rodada_junta_as_novas_de_todos_os_sites_na_ordem_dos_sites():
    store = StateStore()
    world = World()
    sites = [make_site("a.com"), make_site("b.com")]
    discover_all(sites, sources_for(world), store, clock=clock_from(T0))
    world.pages["a.com"].append("https://a.com/2")
    world.pages["b.com"].append("https://b.com/3")

    result = discover_all(
        sites, sources_for(world), store, clock=clock_from(T0 + timedelta(hours=8))
    )

    assert result.run_id == 2
    assert [u.url for u in result.new_urls] == ["https://a.com/2", "https://b.com/3"]
    assert store.count_urls(kind=UrlKind.NEW) == 2


def test_falha_inesperada_em_um_site_nao_derruba_os_demais():
    store = StateStore()
    sites = [make_site("a.com"), make_site("quebra.com"), make_site("b.com")]

    result = discover_all(sites, sources_for(World()), store, clock=clock_from(T0))

    assert [r.site_key for r in result.records] == ["a.com", "quebra.com", "b.com"]
    assert result.records[1].status == "error"
    assert "RuntimeError" in result.records[1].detail
    assert result.records[0].status == result.records[2].status == "ok"


def test_progresso_e_avisado_a_cada_site_concluido():
    store = StateStore()
    sites = [make_site("a.com"), make_site("b.com")]
    seen: list[tuple[int, int]] = []

    discover_all(
        sites,
        sources_for(World()),
        store,
        workers=1,
        clock=clock_from(T0),
        on_progress=lambda done, total, record: seen.append((done, total)),
    )

    assert seen == [(1, 2), (2, 2)]
