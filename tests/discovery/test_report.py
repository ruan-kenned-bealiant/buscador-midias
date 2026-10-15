import csv
from datetime import UTC, datetime
from pathlib import Path

from buscador_midias.discovery.probe import ProbeStatus, SiteProbe
from buscador_midias.discovery.report import probe_all, summarize, write_probe_report
from buscador_midias.domain.site import Site
from buscador_midias.net.fetcher import FetchResult

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def make_site(url: str) -> Site:
    return Site.create(
        name=url,
        url=url,
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def make_probe(key: str, status: ProbeStatus, **kwargs: object) -> SiteProbe:
    defaults: dict[str, object] = {
        "sitemap_urls": (),
        "has_index": False,
        "has_news": False,
        "urls_sampled": 0,
        "with_lastmod": 0,
        "recent_24h": 0,
        "recent_7d": 0,
        "detail": "",
    }
    defaults.update(kwargs)
    return SiteProbe(site_key=key, status=status, **defaults)  # type: ignore[arg-type]


def test_summarize_conta_por_status_e_capacidades():
    probes = [
        make_probe("a.com", ProbeStatus.OK, has_news=True, recent_24h=3, with_lastmod=5),
        make_probe("b.com", ProbeStatus.OK, recent_24h=0, with_lastmod=0, urls_sampled=4),
        make_probe("c.com", ProbeStatus.NO_SITEMAP),
        make_probe("d.com", ProbeStatus.BLOCKED),
    ]

    summary = summarize(probes)

    assert summary["total"] == 4
    assert summary["ok"] == 2
    assert summary["no_sitemap"] == 1
    assert summary["blocked"] == 1
    assert summary["error"] == 0
    assert summary["with_news_sitemap"] == 1
    assert summary["with_recent_24h"] == 1
    assert summary["with_lastmod"] == 1


def test_write_probe_report_gera_csv_legivel(tmp_path: Path):
    path = tmp_path / "relatorio.csv"
    probes = [
        make_probe(
            "a.com",
            ProbeStatus.OK,
            sitemap_urls=("https://a.com/s.xml", "https://a.com/n.xml"),
            recent_24h=2,
        ),
        make_probe("b.com", ProbeStatus.ERROR, detail="sitemap inválido: XML inválido"),
    ]

    write_probe_report(probes, path)

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["site_key"] for r in rows] == ["a.com", "b.com"]
    assert rows[0]["urls_other_hosts"] == "0"
    assert rows[0]["status"] == "ok"
    assert rows[0]["sitemap_urls"] == "https://a.com/s.xml | https://a.com/n.xml"
    assert rows[0]["recent_24h"] == "2"
    assert rows[1]["detail"] == "sitemap inválido: XML inválido"


class SelectiveFetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        if "quebra.com" in url:
            raise RuntimeError("bug inesperado")
        return FetchResult(url=url, status=404, content=b"", error=None)


def test_probe_all_avisa_o_progresso_a_cada_site_concluido():
    sites = [make_site("https://a.com"), make_site("https://b.com"), make_site("https://c.com")]
    seen: list[tuple[int, int, str]] = []

    probe_all(
        sites,
        SelectiveFetcher(),
        now=NOW,
        user_agent="Bot",
        workers=1,
        on_progress=lambda done, total, probe: seen.append((done, total, probe.site_key)),
    )

    assert [(d, t) for d, t, _ in seen] == [(1, 3), (2, 3), (3, 3)]
    assert {key for _, _, key in seen} == {"a.com", "b.com", "c.com"}


def test_probe_all_mantem_ordem_e_isola_falhas_inesperadas():
    sites = [
        make_site("https://a.com"),
        make_site("https://quebra.com"),
        make_site("https://c.com"),
    ]

    probes = probe_all(sites, SelectiveFetcher(), now=NOW, user_agent="Bot", workers=2)

    assert [p.site_key for p in probes] == ["a.com", "quebra.com", "c.com"]
    assert probes[0].status is ProbeStatus.NO_SITEMAP
    assert probes[1].status is ProbeStatus.ERROR
    assert "RuntimeError" in probes[1].detail
    assert probes[2].status is ProbeStatus.NO_SITEMAP
