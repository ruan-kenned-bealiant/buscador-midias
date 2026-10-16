"""Dicas de endereços por fonte, vindas de um levantamento em CSV (palpite, nunca verdade)."""

from pathlib import Path

from tests.strategy.helpers import make_site

from buscador_midias.strategy.hints import SourceHints, load_hints

HEADER = "nome,dominio,news_sitemap_url,rss_url,sitemap_url,outra_coluna\n"


def write(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "dicas.csv"
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_le_as_dicas_da_fonte_na_ordem_sitemap_de_noticias_feed_sitemap(tmp_path: Path):
    hints = load_hints(
        write(
            tmp_path,
            "A,exemplo.com,https://exemplo.com/news.xml,https://exemplo.com/feed,"
            "https://exemplo.com/sitemap.xml,x",
        )
    )

    assert hints.for_site(make_site("https://www.exemplo.com")) == (
        "https://exemplo.com/news.xml",
        "https://exemplo.com/feed",
        "https://exemplo.com/sitemap.xml",
    )


def test_celulas_vazias_e_fonte_sem_linha_dao_tupla_vazia(tmp_path: Path):
    hints = load_hints(write(tmp_path, "A,exemplo.com,,https://exemplo.com/feed,,x"))

    assert hints.for_site(make_site()) == ("https://exemplo.com/feed",)
    assert hints.for_site(make_site("https://outro.com")) == ()


def test_dominio_com_secao_casa_com_a_chave_da_fonte(tmp_path: Path):
    hints = load_hints(write(tmp_path, "A,Exemplo.com/Secao/,https://exemplo.com/secao/n.xml,,,x"))

    assert hints.for_site(make_site("https://exemplo.com/secao")) == (
        "https://exemplo.com/secao/n.xml",
    )
    assert hints.for_site(make_site("https://exemplo.com")) == ()


def test_linhas_repetidas_somam_sem_repetir_endereco(tmp_path: Path):
    hints = load_hints(
        write(
            tmp_path,
            "A,exemplo.com,https://exemplo.com/n.xml,,,x",
            "B,www.exemplo.com,https://exemplo.com/n.xml,https://exemplo.com/feed,,x",
        )
    )

    assert hints.for_site(make_site()) == ("https://exemplo.com/n.xml", "https://exemplo.com/feed")


def test_descarta_endereco_de_outro_dominio_e_link_assinado_com_credencial(tmp_path: Path):
    hints = load_hints(
        write(
            tmp_path,
            "A,exemplo.com,https://sequestrado.example/news.xml,https://exemplo.com/feed,"
            "https://exemplo.com/s.xml?X-Amz-Signature=abc&X-Amz-Credential=xyz,x",
        )
    )

    assert hints.for_site(make_site()) == ("https://exemplo.com/feed",)


def test_aceita_subdominio_da_fonte(tmp_path: Path):
    hints = load_hints(write(tmp_path, "A,exemplo.com,https://admin.exemplo.com/news.xml,,,x"))

    assert hints.for_site(make_site()) == ("https://admin.exemplo.com/news.xml",)


def test_sem_arquivo_nao_ha_dicas(tmp_path: Path):
    assert load_hints(tmp_path / "nao-existe.csv").for_site(make_site()) == ()
    assert SourceHints({}).for_site(make_site()) == ()


def test_junta_varios_arquivos_de_dicas_sem_repetir(tmp_path: Path):
    first = write(tmp_path, "A,exemplo.com,https://exemplo.com/n.xml,,,x")
    second = tmp_path / "manuais.csv"
    second.write_text(
        HEADER
        + "A,exemplo.com,,https://exemplo.com/rss/geral,,x\n"
        + "A,exemplo.com,https://exemplo.com/n.xml,https://exemplo.com/rss/economia,,x\n",
        encoding="utf-8",
    )

    hints = load_hints([first, second])

    assert hints.for_site(make_site()) == (
        "https://exemplo.com/n.xml",
        "https://exemplo.com/rss/geral",
        "https://exemplo.com/rss/economia",
    )


def test_guarda_a_referencia_do_levantamento_por_fonte(tmp_path: Path):
    path = tmp_path / "dicas.csv"
    path.write_text(
        "nome,dominio,news_sitemap_url,rss_url,sitemap_url,categoria,estrategia_recomendada,"
        "itens_24h_estimado_feed,gdelt_artigos_24h_estimado\n"
        "A,exemplo.com,,,,C - Sitemap genérico,sitemap,1000,2298\n",
        encoding="utf-8",
    )

    ref = load_hints(path).reference(make_site())

    assert ref == {
        "categoria": "C - Sitemap genérico",
        "estrategia": "sitemap",
        "itens_24h_feed": "1000",
        "gdelt_24h": "2298",
    }
    assert load_hints(path).reference(make_site("https://outro.com")) == {}


def test_secao_aceita_dica_do_proprio_dominio_fora_do_prefixo(tmp_path: Path):
    """O feed do g1 Acre mora em /rss/g1/ac/acre/, fora de /ac/acre, mas lista matérias de lá."""
    hints = load_hints(
        write(
            tmp_path,
            "A,exemplo.com/ac/acre,,https://exemplo.com/rss/ac/acre/,,x",
            "A,exemplo.com/ac/acre,,https://outro.com/rss/,,x",
        )
    )

    assert hints.for_site(make_site("https://exemplo.com/ac/acre")) == (
        "https://exemplo.com/rss/ac/acre/",
    )


def test_csv_com_colunas_alinhadas_por_espacos_continua_valendo(tmp_path: Path):
    """Editores que alinham o CSV enchem cabeçalho e células de espaços; não some a dica."""
    path = tmp_path / "alinhado.csv"
    path.write_text(
        "nome   , dominio     , news_sitemap_url, rss_url                   , sitemap_url\r\n"
        "g1     , g1.globo.com,                 , https://g1.globo.com/rss/ , \r\n",
        encoding="utf-8",
    )

    hints = load_hints(path)

    assert hints.for_site(make_site("https://g1.globo.com")) == ("https://g1.globo.com/rss/",)
