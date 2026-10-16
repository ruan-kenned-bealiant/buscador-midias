import sqlite3
from pathlib import Path

import pytest

from buscador_midias.state.migrate import MigrationError, migrate_sqlite
from buscador_midias.state.store import StateStore, UrlKind
from conftest import LEGACY, TS


def test_migra_as_tabelas_com_os_tipos_do_postgres(legacy: Path):
    store = StateStore()

    counts = migrate_sqlite(legacy, store)

    assert counts["urls_seen"] == 3 and counts["runs"] == 1
    assert store.count_urls() == 3
    [run] = store.list_runs()
    assert (run.id, run.bright_data_requests, run.bright_data_hosts) == (7, 5, 2)
    analysis = store.get_analysis("h-1")
    assert analysis is not None
    assert analysis.finding is True
    assert analysis.themes == ("fiscal", "criminal")
    assert analysis.entities[0]["name"] == "João Silva"
    evidence = store.get_evidence("h-1")
    assert evidence is not None and evidence.rejected_reasons == ("thin_content", "paywall")
    assert store.corpus_write_counts() == {"written": 1, "created": 1}
    assert store.fallback_hosts() == frozenset({"apnews.com"})
    [site_run] = store.site_runs(7)
    assert (site_run.source, site_run.filtered_urls) == ("feed", 1)
    assert store.enrich_runs()[0].totals.cost_usd == pytest.approx(0.0706)


def test_preserva_ids_das_rodadas_e_a_ordem_de_chegada_das_urls(legacy: Path):
    store = StateStore()

    migrate_sqlite(legacy, store)

    assert [r.id for r in store.enrich_runs()] == [3]
    order = [row[0] for row in store._all("SELECT url_hash FROM midias_crawler_urls ORDER BY id")]
    assert order == ["h-1", "h-0", "h-2"]  # a ordem do rowid original


def test_novas_rodadas_continuam_depois_do_maior_id_migrado(legacy: Path):
    from datetime import UTC, datetime

    store = StateStore()
    migrate_sqlite(legacy, store)

    assert store.start_run(datetime(2026, 10, 16, tzinfo=UTC)) == 8
    assert (
        store.start_enrich_run(
            datetime(2026, 10, 16, tzinfo=UTC), model="m", prompt_version="v", evidence_mode="auto"
        )
        == 4
    )


def test_texto_com_caractere_nulo_migra_sem_ele(legacy: Path):
    store = StateStore()

    migrate_sqlite(legacy, store)

    [recent] = [r for r in store.recent_new_urls(10) if r.url.endswith("/um")]
    assert recent.title == "Títulosujo"


def test_contadores_e_pendencias_batem_depois_da_migracao(legacy: Path):
    store = StateStore()

    migrate_sqlite(legacy, store)

    assert store.count_urls_by_kind() == {"new": 1, "baseline": 1}
    assert store.count_urls(kind=UrlKind.FILTERED) == 1
    assert store.filtered_by_reason() == {"listing": 1}


def test_recusa_migrar_para_um_estado_que_ja_tem_dados(legacy: Path):
    store = StateStore()
    migrate_sqlite(legacy, store)

    with pytest.raises(MigrationError) as error:
        migrate_sqlite(legacy, store)

    assert "já tem dados" in str(error.value)
    assert store.count_urls() == 3  # nada foi duplicado


def test_arquivo_inexistente_ou_que_nao_e_do_buscador_e_recusado(tmp_path: Path):
    store = StateStore()
    other = tmp_path / "outro.db"
    sqlite3.connect(other).executescript("CREATE TABLE x (a INTEGER);")

    with pytest.raises(MigrationError):
        migrate_sqlite(tmp_path / "nao-existe.db", store)
    with pytest.raises(MigrationError):
        migrate_sqlite(other, store)


def test_banco_de_antes_da_coluna_source_migra_com_fonte_vazia(tmp_path: Path):
    path = tmp_path / "velho.db"
    db = sqlite3.connect(path)
    db.executescript(LEGACY.replace("source TEXT NOT NULL DEFAULT '', ", ""))
    db.executescript(
        f"""
        INSERT INTO runs VALUES (1, '{TS}', NULL, 0, 0);
        INSERT INTO site_runs VALUES (1, 'a.com', 'ok', 0, 0, 0, 0, 1, 0, '', 0, 0, 0);
        """
    )
    db.commit()
    db.close()
    store = StateStore()

    migrate_sqlite(path, store)

    assert store.site_runs(1)[0].source == ""
