import hashlib

import pytest

from buscador_midias.evidence.storage import LocalEvidenceStore

HASH = "ab" * 32


def test_grava_no_mesmo_caminho_relativo_do_kyx(tmp_path):
    store = LocalEvidenceStore(tmp_path)

    path = store.put(HASH, b"<html>x</html>")

    assert path == f"midias-ia/evidencias/{HASH}.html"
    assert (tmp_path / path).read_bytes() == b"<html>x</html>"


def test_prefixo_e_configuravel(tmp_path):
    store = LocalEvidenceStore(tmp_path, prefix="outro/prefixo")

    assert store.put(HASH, b"x") == f"outro/prefixo/{HASH}.html"


def test_regravar_substitui_o_conteudo_sem_deixar_arquivo_temporario(tmp_path):
    store = LocalEvidenceStore(tmp_path)
    store.put(HASH, b"primeiro")

    store.put(HASH, b"segundo")

    folder = tmp_path / "midias-ia" / "evidencias"
    assert [p.name for p in folder.iterdir()] == [f"{HASH}.html"]
    assert (folder / f"{HASH}.html").read_bytes() == b"segundo"


@pytest.mark.parametrize("bad", ["../fora", "a/b", "", "a b", "x\x00y", ".."])
def test_hash_inseguro_e_recusado_para_nao_escapar_da_pasta(tmp_path, bad):
    with pytest.raises(ValueError, match="url_hash"):
        LocalEvidenceStore(tmp_path).put(bad, b"x")

    assert list(tmp_path.rglob("*.html")) == []


def test_le_de_volta_e_confere_o_sha256(tmp_path):
    store = LocalEvidenceStore(tmp_path)
    store.put(HASH, b"conteudo")

    assert store.read(f"midias-ia/evidencias/{HASH}.html") == b"conteudo"
    assert (
        hashlib.sha256(store.read(f"midias-ia/evidencias/{HASH}.html")).hexdigest()
        == hashlib.sha256(b"conteudo").hexdigest()
    )


def test_leitura_nao_sai_da_raiz(tmp_path):
    with pytest.raises(ValueError, match="caminho"):
        LocalEvidenceStore(tmp_path).read("../../etc/passwd")


def test_compaction_preserves_key_hash_and_allows_replacing_archived_copy(tmp_path, monkeypatch):
    import importlib
    import time
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    compressor = importlib.import_module("compact_old_evidence")
    monkeypatch.setattr(compressor, "ROOT", tmp_path)
    store = LocalEvidenceStore(tmp_path)
    body = b"<html>conteudo antigo verificavel</html>" * 100
    key = store.put(HASH, body)
    path = tmp_path / key
    result = compressor.compact(path, time.time() + 1)
    assert result and not path.exists()
    assert (tmp_path / (key + ".gz")).exists()
    assert store.read(key) == body
    assert hashlib.sha256(store.read(key)).hexdigest() == result["sha256"]
    store.put(HASH, b"captura mais recente")
    assert store.read(key) == b"captura mais recente"
