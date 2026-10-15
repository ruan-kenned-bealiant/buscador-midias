from buscador_midias.discovery.robots import RobotsPolicy

UA = "BuscadorMidiasBot"

ROBOTS = """User-agent: *
Disallow: /admin/
Disallow: /busca

User-agent: BadBot
Disallow: /

Sitemap: https://exemplo.com/sitemap_index.xml
sitemap: https://exemplo.com/news-sitemap.xml
"""


def test_extrai_sitemaps_declarados_ignorando_caixa():
    policy = RobotsPolicy.parse(ROBOTS)

    assert policy.sitemaps == (
        "https://exemplo.com/sitemap_index.xml",
        "https://exemplo.com/news-sitemap.xml",
    )


def test_respeita_disallow_do_agente_curinga():
    policy = RobotsPolicy.parse(ROBOTS)

    assert policy.allows("https://exemplo.com/politica/x", UA)
    assert not policy.allows("https://exemplo.com/admin/painel", UA)
    assert not policy.allows("https://exemplo.com/busca?q=a", UA)


def test_respeita_regra_especifica_do_agente():
    policy = RobotsPolicy.parse(ROBOTS)

    assert not policy.allows("https://exemplo.com/", "BadBot")


def test_vazio_permite_tudo_e_nao_tem_sitemaps():
    policy = RobotsPolicy.parse("")

    assert policy.sitemaps == ()
    assert policy.allows("https://exemplo.com/qualquer", UA)


def test_resolve_sitemap_relativo_contra_a_base():
    policy = RobotsPolicy.parse(
        "Sitemap: /sitemap.xml\nSitemap: sitemap2.xml\n", base_url="https://exemplo.com/"
    )

    assert policy.sitemaps == (
        "https://exemplo.com/sitemap.xml",
        "https://exemplo.com/sitemap2.xml",
    )


def test_descarta_sitemap_com_esquema_invalido():
    policy = RobotsPolicy.parse(
        "Sitemap: hthttps://x.com/s.xml\nSitemap: https://ok.com/s.xml\n",
        base_url="https://x.com",
    )

    assert policy.sitemaps == ("https://ok.com/s.xml",)


def test_sitemap_relativo_sem_base_e_descartado():
    assert RobotsPolicy.parse("Sitemap: /sitemap.xml\n").sitemaps == ()


def test_allow_all_ajuda_quando_robots_ausente():
    policy = RobotsPolicy.allow_all()

    assert policy.allows("https://exemplo.com/x", UA)
    assert policy.sitemaps == ()
