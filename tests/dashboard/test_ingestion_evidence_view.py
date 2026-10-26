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


def test_compressed_copy_preserves_bytes_and_limits_inflation(tmp_path, monkeypatch):
    import gzip

    import buscador_midias.ingestion.evidence_view as view

    target = tmp_path / (KEY + ".gz")
    target.parent.mkdir(parents=True)
    body = b"<html>Noticia antiga preservada</html>"
    target.write_bytes(gzip.compress(body))
    assert read_local(KEY, [tmp_path]) == body
    monkeypatch.setattr(view, "MAX_BYTES", len(body) - 1)
    with pytest.raises(CatalogError) as error:
        read_local(KEY, [tmp_path])
    assert error.value.status == 413


def test_compressed_symlink_cannot_escape_root(tmp_path):
    import gzip

    root = tmp_path / "root"
    target = root / (KEY + ".gz")
    target.parent.mkdir(parents=True)
    outside = tmp_path / "private.gz"
    outside.write_bytes(gzip.compress(b"private"))
    target.symlink_to(outside)
    with pytest.raises(CatalogError):
        read_local(KEY, [root])
