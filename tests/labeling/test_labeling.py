import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.labeling.export import export_batch
from buscador_midias.labeling.score import score_labels, wilson_interval
from buscador_midias.state.store import (
    AnalysisRecord,
    ContentRecord,
    EvidenceRecord,
    StateStore,
    UrlKind,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def found(path: str, site: str = "a.com") -> DiscoveredUrl:
    return DiscoveredUrl(
        f"https://{site}/{path}", f"h-{path}", site, "s", None, f"Titulo {path}", None
    )


def build_store(findings: int = 4, no_entity: int = 4, with_text: bool = True) -> StateStore:
    store = StateStore()
    run = store.start_run(NOW)
    enrich = store.start_enrich_run(NOW, model="m", prompt_version="v", evidence_mode="auto")
    groups = {"f": (findings, True), "k": (no_entity, False)}
    for tag, (count, is_finding) in groups.items():
        paths = [f"{tag}-{n}-materia" for n in range(count)]
        store.record_urls([found(p) for p in paths], run_id=run, now=NOW, kind=UrlKind.NEW)
        for p in paths:
            if with_text:
                store.record_content(
                    ContentRecord(
                        f"h-{p}",
                        "ok",
                        200,
                        None,
                        f"Manchete {p}",
                        "Desc",
                        "Trecho da matéria " * 60,
                        900,
                        NOW,
                        "pt-BR",
                        None,
                    ),
                    now=NOW,
                )
            entities = (
                ({"name": "João Silva", "type": "PF", "role": "acusado"},) if is_finding else ()
            )
            store.record_analysis(
                AnalysisRecord(
                    f"h-{p}",
                    is_finding,
                    ("fiscal",) if is_finding else (),
                    entities,
                    "fato" if is_finding else "",
                    "r",
                    0.9,
                    "text",
                ),
                enrich_run_id=enrich,
                model="m",
                prompt_version="v",
                now=NOW,
            )
            if is_finding:
                store.record_evidence(
                    EvidenceRecord(
                        f"h-{p}",
                        "captured",
                        "direct",
                        "p",
                        "ab" * 32,
                        1,
                        "text/html",
                        200,
                        None,
                        (),
                        None,
                    ),
                    now=NOW,
                )
    return store


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_lote_tem_os_dois_grupos_e_o_csv_nao_revela_o_veredito_do_modelo(tmp_path: Path):
    store = build_store()

    batch = export_batch(store, tmp_path, per_group=3, seed=1)

    assert batch.number == 1
    assert batch.counts == {"in_corpus": 3, "no_entity": 3}
    blind = read(tmp_path / "rotulagem-01.csv")
    assert len(blind) == 6
    forbidden = {"grupo", "achado", "temas", "entidades", "fato", "evidencia", "modelo"}
    assert forbidden.isdisjoint({column.lower() for column in blind[0]})
    assert all(row["veredito"] == "" for row in blind)
    assert {
        "id",
        "fonte",
        "url",
        "manchete",
        "trecho",
        "veredito",
        "entidades_nomeadas",
        "obs",
    } <= set(blind[0])
    text = (tmp_path / "rotulagem-01.csv").read_text(encoding="utf-8-sig")
    assert "fiscal" not in text and "João Silva" not in text


def test_chave_guarda_o_grupo_e_o_veredito_do_modelo(tmp_path: Path):
    store = build_store()

    export_batch(store, tmp_path, per_group=2, seed=1)

    key = read(tmp_path / "chave.csv")
    assert {row["grupo"] for row in key} == {"in_corpus", "no_entity"}
    finding = next(row for row in key if row["grupo"] == "in_corpus")
    assert finding["temas"] == "fiscal"
    assert finding["entidades"] == "João Silva"
    assert finding["evidencia"] == "direct"
    assert {row["id"] for row in key} == {row["id"] for row in read(tmp_path / "rotulagem-01.csv")}


def test_ordem_do_csv_e_embaralhada_para_nao_agrupar_por_veredito(tmp_path: Path):
    store = build_store(findings=10, no_entity=10)

    export_batch(store, tmp_path, per_group=10, seed=3)

    key = {row["id"]: row["grupo"] for row in read(tmp_path / "chave.csv")}
    order = [key[row["id"]] for row in read(tmp_path / "rotulagem-01.csv")]
    assert order != sorted(order)  # misturados


def test_segundo_lote_nao_repete_urls_e_continua_a_numeracao(tmp_path: Path):
    store = build_store(findings=6, no_entity=6)
    first = export_batch(store, tmp_path, per_group=3, seed=1)

    second = export_batch(store, tmp_path, per_group=3, seed=2)

    assert (first.number, second.number) == (1, 2)
    a = {row["url"] for row in read(tmp_path / "rotulagem-01.csv")}
    b = {row["url"] for row in read(tmp_path / "rotulagem-02.csv")}
    assert a.isdisjoint(b)
    ids = [row["id"] for row in read(tmp_path / "chave.csv")]
    assert len(ids) == len(set(ids)) == 12


def test_grupo_com_menos_itens_que_o_pedido_exporta_o_que_existe(tmp_path: Path):
    store = build_store(findings=2, no_entity=5)

    batch = export_batch(store, tmp_path, per_group=4, seed=1)

    assert batch.counts["in_corpus"] == 2
    assert batch.counts["no_entity"] == 4


def test_trecho_vem_do_texto_baixado_e_e_limitado(tmp_path: Path):
    store = build_store()

    export_batch(store, tmp_path, per_group=1, seed=1)

    rows = read(tmp_path / "rotulagem-01.csv")
    with_text = [r for r in rows if "Trecho da matéria" in r["trecho"]]
    assert with_text and all(len(r["trecho"]) <= 650 for r in rows)


def test_sem_texto_baixado_o_trecho_avisa_que_so_ha_a_manchete(tmp_path: Path):
    store = build_store(findings=2, no_entity=2, with_text=False)

    export_batch(store, tmp_path, per_group=2, seed=1)

    rows = read(tmp_path / "rotulagem-01.csv")
    assert {r["trecho"] for r in rows} == {"(texto não baixado: só a manchete)"}


def test_mesma_semente_gera_o_mesmo_lote(tmp_path: Path):
    one, two = tmp_path / "um", tmp_path / "dois"
    store = build_store(findings=8, no_entity=8)

    export_batch(store, one, per_group=4, seed=5)
    export_batch(store, two, per_group=4, seed=5)

    assert read(one / "rotulagem-01.csv") == read(two / "rotulagem-01.csv")


def fill(folder: Path, verdicts: dict[str, str], batch: str = "rotulagem-01.csv") -> None:
    rows = read(folder / batch)
    key = {row["id"]: row for row in read(folder / "chave.csv")}
    with (folder / batch).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            row["veredito"] = verdicts.get(key[row["id"]]["grupo"], "")
            writer.writerow(row)


def test_pontuacao_calcula_precisao_e_recall_estimado_da_analise(tmp_path: Path):
    store = build_store(findings=10, no_entity=10)
    export_batch(store, tmp_path, per_group=10, seed=1)
    rows = read(tmp_path / "rotulagem-01.csv")
    key = {row["id"]: row["grupo"] for row in read(tmp_path / "chave.csv")}
    verdicts = {}
    counters = {"in_corpus": 0, "no_entity": 0}
    plan = {"in_corpus": 8, "no_entity": 2}  # quantos "sim" por grupo
    for row in rows:
        group = key[row["id"]]
        verdicts[row["id"]] = "sim" if counters[group] < plan[group] else "nao"
        counters[group] += 1
    _fill_by_id(tmp_path, verdicts)

    report = score_labels(store, tmp_path)

    assert report.groups["in_corpus"].labeled == 10
    assert report.groups["in_corpus"].yes_rate == 0.8
    assert report.groups["no_entity"].yes_rate == 0.2
    # população: 10 com entidade e 10 sem -> deveriam entrar 8 e 2; a análise achou 8 de 10
    assert report.corpus_precision == 0.8
    assert report.analysis_recall == pytest.approx(8 / (8 + 2))


def _fill_by_id(folder: Path, verdicts: dict[str, str]) -> None:
    rows = read(folder / "rotulagem-01.csv")
    with (folder / "rotulagem-01.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            row["veredito"] = verdicts.get(row["id"], "")
            writer.writerow(row)


def test_incertos_e_em_branco_ficam_fora_do_denominador_mas_sao_contados(tmp_path: Path):
    store = build_store(findings=4, no_entity=0)
    export_batch(store, tmp_path, per_group=4, seed=1)
    ids = [row["id"] for row in read(tmp_path / "rotulagem-01.csv")]
    _fill_by_id(tmp_path, {ids[0]: "sim", ids[1]: "nao", ids[2]: "incerto"})  # ids[3] em branco

    group = score_labels(store, tmp_path).groups["in_corpus"]

    assert (group.yes, group.no, group.uncertain, group.unlabeled) == (1, 1, 1, 1)
    assert group.yes_rate == 0.5
    assert group.labeled == 2


def test_leitor_aceita_ponto_e_virgula_cp1252_e_sinonimos(tmp_path: Path):
    store = build_store(findings=3, no_entity=0)
    export_batch(store, tmp_path, per_group=3, seed=1)
    rows = read(tmp_path / "rotulagem-01.csv")
    values = ["Sim", "não", "  ENTRA "]
    text = ";".join(rows[0].keys()) + "\n"
    for row, value in zip(rows, values, strict=True):
        row["veredito"] = value
        text += ";".join(row[k].replace(";", ",").replace("\n", " ") for k in rows[0]) + "\n"
    (tmp_path / "rotulagem-01.csv").write_bytes(text.encode("cp1252", errors="replace"))

    group = score_labels(store, tmp_path).groups["in_corpus"]

    assert (group.yes, group.no) == (2, 1)


def test_ids_desconhecidos_sao_ignorados_e_contados(tmp_path: Path):
    store = build_store(findings=2, no_entity=0)
    export_batch(store, tmp_path, per_group=2, seed=1)
    rows = read(tmp_path / "rotulagem-01.csv")
    rows.append({**rows[0], "id": "L9999", "veredito": "sim"})
    with (tmp_path / "rotulagem-01.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    report = score_labels(store, tmp_path)

    assert report.unknown_ids == 1


def test_sem_rotulos_nao_ha_taxas_nem_recalls(tmp_path: Path):
    store = build_store()
    export_batch(store, tmp_path, per_group=2, seed=1)

    report = score_labels(store, tmp_path)

    assert report.corpus_precision is None
    assert report.analysis_recall is None
    assert report.groups["in_corpus"].yes_rate is None


def test_intervalo_de_wilson():
    low, high = wilson_interval(8, 10)

    assert 0.48 < low < 0.50 and 0.93 < high < 0.95  # Wilson 8/10 = (0.490, 0.943)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(10, 10)
    assert high == 1.0 and low > 0.7
