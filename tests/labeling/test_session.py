import csv
import threading
from dataclasses import fields
from pathlib import Path

import pytest
from test_labeling import build_store  # fixtures do módulo irmão

from buscador_midias.labeling.export import export_batch
from buscador_midias.labeling.score import score_labels
from buscador_midias.labeling.session import LabelSession


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    store = build_store(findings=3, no_entity=3)
    export_batch(store, tmp_path, per_group=3, seed=1)
    return tmp_path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_lista_so_os_campos_cegos_na_ordem_do_arquivo(folder: Path):
    items = LabelSession(folder).items()

    assert [i.id for i in items] == [r["id"] for r in rows(folder / "rotulagem-01.csv")]
    assert len(items) == 6
    first = items[0]
    assert first.url.startswith("https://a.com/") and first.source == "a.com"
    assert first.verdict == ""
    assert {f.name for f in fields(first)} == {
        "id",
        "source",
        "url",
        "headline",
        "excerpt",
        "verdict",
    }


def test_grava_o_veredito_no_csv_e_a_pontuacao_le(folder: Path):
    session = LabelSession(folder)
    for item in session.items():
        session.set_verdict(item.id, "sim")

    assert {r["veredito"] for r in rows(folder / "rotulagem-01.csv")} == {"sim"}
    store = build_store(findings=3, no_entity=3)
    report = score_labels(store, folder)
    assert report.groups["in_corpus"].yes == 3
    assert report.groups["in_corpus"].unlabeled == 0


def test_aceita_os_tres_vereditos_e_limpa_com_vazio(folder: Path):
    session = LabelSession(folder)
    a, b, c = (i.id for i in session.items()[:3])

    session.set_verdict(a, "sim")
    session.set_verdict(b, "nao")
    session.set_verdict(c, "incerto")
    by_id = {i.id: i.verdict for i in session.items()}
    assert (by_id[a], by_id[b], by_id[c]) == ("sim", "nao", "incerto")

    session.set_verdict(a, "")
    assert {i.id: i.verdict for i in session.items()}[a] == ""


def test_so_a_linha_alterada_muda_e_as_outras_colunas_ficam(folder: Path):
    path = folder / "rotulagem-01.csv"
    before = rows(path)
    before[1]["obs"] = "anotação minha"
    before[1]["entidades_nomeadas"] = "João"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(before[0]))
        writer.writeheader()
        writer.writerows(before)

    LabelSession(folder).set_verdict(before[0]["id"], "nao")

    after = rows(path)
    assert after[0]["veredito"] == "nao"
    assert after[1] == before[1]
    assert [r["trecho"] for r in after] == [r["trecho"] for r in before]
    assert list(after[0]) == list(before[0])


def test_id_desconhecido_e_veredito_invalido_sao_recusados(folder: Path):
    session = LabelSession(folder)
    known = session.items()[0].id

    with pytest.raises(KeyError):
        session.set_verdict("L9999", "sim")
    with pytest.raises(ValueError, match="veredito"):
        session.set_verdict(known, "adverso")  # palavra do vocabulário antigo: não vale mais
    assert session.items()[0].verdict == ""


def test_mostra_rotulos_ja_escritos_a_mao_com_sinonimos(folder: Path):
    path = folder / "rotulagem-01.csv"
    data = rows(path)
    data[0]["veredito"] = "Sim"
    data[1]["veredito"] = "NÃO"
    data[2]["veredito"] = "talvez"
    data[3]["veredito"] = "qualquer coisa"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)

    verdicts = [i.verdict for i in LabelSession(folder).items()[:4]]

    assert verdicts == ["sim", "nao", "incerto", ""]


def test_le_e_regrava_csv_do_excel_em_cp1252_com_ponto_e_virgula(tmp_path: Path):
    header = "id;fonte;url;manchete;trecho;veredito;entidades_nomeadas;obs\r\n"
    line = "L0001;a.com;https://a.com/x;Ação contra João;Trecho çãõ;;;\r\n"
    (tmp_path / "rotulagem-01.csv").write_bytes((header + line).encode("cp1252"))
    session = LabelSession(tmp_path)

    assert session.items()[0].headline == "Ação contra João"
    session.set_verdict("L0001", "sim")

    saved = rows(tmp_path / "rotulagem-01.csv")
    assert saved[0]["manchete"] == "Ação contra João"
    assert saved[0]["trecho"] == "Trecho çãõ"
    assert saved[0]["veredito"] == "sim"


def test_varios_lotes_aparecem_juntos_e_cada_voto_vai_para_o_seu_arquivo(folder: Path):
    store = build_store(findings=3, no_entity=3)
    export_batch(store, folder, per_group=3, seed=2)  # nada novo: as URLs já foram exportadas
    second = folder / "rotulagem-02.csv"
    header = rows(folder / "rotulagem-01.csv")[0]
    extra = dict.fromkeys(header, "")
    extra.update(id="L0100", fonte="b.com", url="https://b.com/y", manchete="Outra")
    with second.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(header))
        writer.writeheader()
        writer.writerow(extra)
    session = LabelSession(folder)

    session.set_verdict("L0100", "incerto")

    assert session.items()[-1].id == "L0100"
    assert rows(second)[0]["veredito"] == "incerto"
    assert {r["veredito"] for r in rows(folder / "rotulagem-01.csv")} == {""}


def test_votos_simultaneos_nao_se_perdem_e_nao_sobra_arquivo_temporario(folder: Path):
    session = LabelSession(folder)
    ids = [i.id for i in session.items()]

    threads = [threading.Thread(target=session.set_verdict, args=(i, "sim")) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {i.verdict for i in session.items()} == {"sim"}
    assert sorted(p.name for p in folder.iterdir()) == ["chave.csv", "rotulagem-01.csv"]


def test_pasta_sem_lotes_da_lista_vazia(tmp_path: Path):
    assert LabelSession(tmp_path).items() == []
