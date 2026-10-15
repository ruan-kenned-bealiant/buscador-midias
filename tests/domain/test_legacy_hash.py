"""Hash legado: o corpus tem linhas hasheadas antes do commit 413fc198f do KyX (2026-10-01).

Até então o PHP removia só o prefixo literal ``www.``; depois passou a remover ``www\\d*.``. Linhas
como ``www1.folha.uol.com.br`` gravadas antes ficaram com o hash antigo. O fixture
(tests/fixtures/corpus_hash_sample.json) traz linhas REAIS do corpus como oráculo.
"""

import hashlib
import json
from pathlib import Path

import pytest

from buscador_midias.domain.url import legacy_url_hash, url_hash, url_hash_candidates

SAMPLE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "corpus_hash_sample.json").read_text(
        encoding="utf-8"
    )
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.noticia.com/artigo",
        "https://noticia.com/artigo",
        "http://WWW.Noticia.com/artigo/?utm_source=x",
        "https://wwwx.noticia.com/artigo",
    ],
)
def test_sem_prefixo_www_numerado_nao_ha_hash_legado_distinto(url: str):
    assert legacy_url_hash(url) is None
    assert url_hash_candidates(url) == (url_hash(url),)


def test_host_com_www_numerado_tem_hash_legado_que_mantem_o_prefixo():
    url = "https://www1.folha.uol.com.br/mundo/2026/08/materia.shtml?utm_source=x"

    assert legacy_url_hash(url) == sha("https://www1.folha.uol.com.br/mundo/2026/08/materia.shtml")
    assert legacy_url_hash(url) != url_hash(url)


def test_candidatos_trazem_o_atual_primeiro_e_depois_o_legado():
    url = "https://www2.noticia.com/artigo/"

    assert url_hash_candidates(url) == (url_hash(url), legacy_url_hash(url))


def test_o_legado_so_difere_do_atual_no_prefixo_do_host():
    url = "http://WWW3.Noticia.com:8080/Artigo/?b=2&a=1&utm_source=x#frag"

    assert legacy_url_hash(url) == sha("https://www3.noticia.com/Artigo?a=1&b=2")


def test_fixture_cobre_os_tres_perfis_de_linha_real():
    legacy_rows = [r for r in SAMPLE if url_hash(r["url"]) != r["url_hash"]]

    assert len(legacy_rows) >= 10
    assert len(SAMPLE) - len(legacy_rows) >= 8


def test_toda_linha_real_do_corpus_e_reconhecida_por_algum_candidato():
    for row in SAMPLE:
        assert row["url_hash"] in url_hash_candidates(row["url"]), row["url"]


def test_linhas_antigas_do_corpus_batem_com_o_hash_legado_e_nao_com_o_atual():
    legacy_rows = [r for r in SAMPLE if url_hash(r["url"]) != r["url_hash"]]

    for row in legacy_rows:
        assert legacy_url_hash(row["url"]) == row["url_hash"], row["url"]
