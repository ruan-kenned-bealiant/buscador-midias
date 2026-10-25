"""Visualização lê a cópia salva e não aceita arquivos fora da raiz de evidências."""

import pytest

from buscador_midias.catalog.store import CatalogError
from buscador_midias.ingestion.evidence_view import read_local

KEY = "midias-ia/evidencias/test.html"


def test_saved_bytes_are_served_unchanged_from_preserved_root(tmp_path):
    current, previous = tmp_path / "current", tmp_path / "previous"
    target = previous / KEY
    target.parent.mkdir(parents=True)
    target.write_bytes(b"<html><body>Noticia preservada</body></html>")
    assert read_local(KEY, [current, previous]) == target.read_bytes()


@pytest.mark.parametrize(
    "key", ["/etc/passwd", "../test.html", "midias-ia/evidencias/../test.html"]
)
def test_arbitrary_paths_are_rejected(tmp_path, key):
    with pytest.raises(CatalogError):
        read_local(key, [tmp_path])


def test_symlink_cannot_escape_evidence_root(tmp_path):
    root = tmp_path / "evidence"
    target = root / KEY
    target.parent.mkdir(parents=True)
    outside = tmp_path / "private.html"
    outside.write_text("private")
    target.symlink_to(outside)
    with pytest.raises(CatalogError):
        read_local(KEY, [root])


def test_streamed_article_is_readable_without_changing_original():
    from buscador_midias.ingestion.evidence_view import preview_html

    paragraph = "Texto já capturado da notícia, com fatos e contexto. " * 6
    original = (
        '<html><head><meta charset="utf-8"><title>Matéria</title></head><body>'
        '<template id="B:0"></template><div>Carregando</div>'
        f'<div hidden id="S:0"><h1>Matéria</h1><p>{paragraph}</p></div>'
        "<script>alert(1)</script></body></html>"
    ).encode()
    preview = preview_html(original, "a" * 64).decode()
    assert "<article>" in preview and paragraph.strip() in preview
    assert "<script" not in preview and "Carregando" not in preview
    assert "?original=1" in preview
    assert b"<div hidden" in original


def test_regular_html_is_not_rewritten():
    from buscador_midias.ingestion.evidence_view import preview_html

    original = b"<html><body><article>Noticia</article></body></html>"
    assert preview_html(original, "a" * 64) == original
