"""Revisão dos links do cadastro em lotes de sites: ``probe`` sonda e relata, ``apply`` executa.

``probe`` lê cada link ao vivo, grava ``report.json`` e ``report.csv`` e não altera o cadastro.
``apply`` aplica, a partir de um relatório já conferido, só as propostas escolhidas (nunca
``review`` nem ``keep``), pelo próprio cadastro, na versão sondada.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from buscador_midias.catalog.http_transport import make_reader as transport_reader
from buscador_midias.catalog.link_review import (
    Reader,
    apply_proposals,
    decide_by_address,
    review_sites,
)
from buscador_midias.catalog.store import CatalogStore

ROOT = Path("var/maintenance/link-review")
APPLICABLE = ("inactivate", "delete", "remove_theme", "remove_historical")
COLUMNS = (
    "site",
    "link_id",
    "url",
    "label",
    "kind",
    "model",
    "proposal",
    "reason",
    "age_days",
    "newest",
    "basis",
    "exact",
    "theme",
    "status",
    "entries",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review-links", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="Sonda um lote de sites e grava o relatório.")
    probe.add_argument("--site-id", type=int, nargs="+", default=[], help="Sites pelo id.")
    probe.add_argument("--name", nargs="+", default=[], help="Sites pelo trecho do nome ou URL.")
    probe.add_argument(
        "--largest",
        type=int,
        default=0,
        help="Mais N sites ativos com mais links, ainda não revisados.",
    )
    probe.add_argument("--per-host", type=int, default=2, help="Links lidos por vez em cada site.")
    commands.add_parser(
        "historical",
        help="Lista os links ativos cujo endereço cobre um período encerrado (não lê a rede).",
    )
    apply = commands.add_parser("apply", help="Aplica propostas escolhidas de um relatório.")
    apply.add_argument("report", type=Path)
    apply.add_argument(
        "--proposals",
        nargs="+",
        required=True,
        choices=APPLICABLE,
        help="Quais propostas executar; review e keep nunca são executadas.",
    )
    return parser


def _reviewed(root: Path) -> set[int]:
    done: set[int] = set()
    for report in root.glob("*/report.json"):
        done.update(site["id"] for site in json.loads(report.read_text())["sites"])
    return done


def _choose(
    store: CatalogStore, ids: list[int], names: list[str], largest: int, root: Path
) -> list[dict[str, Any]]:
    """Resolve o lote: ids e nomes pedidos, mais os maiores ainda não revisados."""
    with store.connection() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT s.id,s.name,s.url,COUNT(l.id) links FROM sites s LEFT JOIN links l "
                "ON l.source_id=s.id AND l.active=1 AND l.deleted_at IS NULL "
                "WHERE s.active=1 AND s.deleted_at IS NULL GROUP BY s.id,s.name,s.url "
                "ORDER BY COUNT(l.id) DESC,s.id"
            ).fetchall()
        ]
    wanted = [
        r
        for r in rows
        if r["id"] in ids or any(n.lower() in f"{r['name']} {r['url']}".lower() for n in names)
    ]
    taken = {r["id"] for r in wanted}
    skip = taken | _reviewed(root)
    wanted += [r for r in rows if r["id"] not in skip][:largest]
    return wanted


def _real_reader(link: dict[str, Any]) -> Reader:
    """Leitor com o transporte que o cadastro já descobriu ser necessário para o link."""
    return transport_reader(
        [link["url"]],
        lambda step: None,
        transport=link.get("access_transport", "direct"),
        seconds=25,
        requests=4,
    )


def _probe(
    args: argparse.Namespace,
    store: CatalogStore,
    make_reader: Callable[[dict[str, Any]], Reader],
    now: datetime,
    root: Path,
) -> int:
    sites = _choose(store, args.site_id, args.name, args.largest, root)
    if not sites:
        print("Nenhum site selecionado.", file=sys.stderr)
        return 2

    def progress(done: int, total: int) -> None:
        if done % 100 == 0 or done == total:
            print(f"{done}/{total} links sondados", file=sys.stderr, flush=True)

    results = review_sites(
        store, [s["id"] for s in sites], now, make_reader, per_host=args.per_host, progress=progress
    )
    return _write(root, now, sites, results)


def _write(
    root: Path, now: datetime, sites: list[dict[str, Any]], results: list[dict[str, Any]]
) -> int:
    """Grava ``report.json`` e ``report.csv`` numa pasta nova da rodada e imprime o caminho."""
    folder = root / f"{now.astimezone(UTC):%Y%m%dT%H%M%S}-{len(sites)}sites"
    folder.mkdir(parents=True, exist_ok=True)
    report = dict(probed_at=now.isoformat(), sites=sites, results=results, applied=None)
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    names = {s["id"]: s["name"] for s in sites}
    with (folder / "report.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for item in results:
            writer.writerow({**item, "site": names[item["source_id"]]})
    print(folder)
    return 0


def _historical(store: CatalogStore, now: datetime, root: Path) -> int:
    """Relatório dos links ativos de período encerrado; decide só pelo endereço, sem rede."""
    with store.connection() as db:
        rows = db.execute(
            "SELECT l.id,l.version,l.source_id,l.url,l.label,l.kind,l.model,l.access_transport,"
            "s.name site_name FROM links l JOIN sites s ON s.id=l.source_id "
            "WHERE l.active=1 AND l.deleted_at IS NULL AND s.active=1 AND s.deleted_at IS NULL "
            "ORDER BY l.source_id,l.id"
        ).fetchall()
    names: dict[int, str] = {}
    results = []
    for row in map(dict, rows):
        found = decide_by_address(row, now)
        if found and found["proposal"] == "remove_historical":
            results.append(found)
            names[row["source_id"]] = row["site_name"]
    sites = [dict(id=i, name=n) for i, n in sorted(names.items())]
    return _write(root, now, sites, results)


def _apply(args: argparse.Namespace, store: CatalogStore) -> int:
    report = json.loads(args.report.read_text())
    if report.get("applied"):
        print("Este relatório já foi aplicado.", file=sys.stderr)
        return 2
    chosen = [r for r in report["results"] if r["proposal"] in args.proposals]
    done = apply_proposals(store, chosen)
    report["applied"] = dict(done, at=datetime.now(UTC).isoformat(), proposals=args.proposals)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps(dict(done), ensure_ascii=False))
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    store: CatalogStore | None = None,
    make_reader: Callable[[dict[str, Any]], Reader] | None = None,
    now: datetime | None = None,
    root: Path = ROOT,
) -> int:
    """Executa ``probe`` ou ``apply``; os parâmetros nomeados existem para os testes injetarem."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "probe" and not (args.site_id or args.name or args.largest):
        parser.error("Informe --site-id, --name ou --largest.")
    if store is None:
        from buscador_midias.catalog.postgres import PostgresCatalogStore

        store = PostgresCatalogStore()
    if args.command == "apply":
        return _apply(args, store)
    if args.command == "historical":
        return _historical(store, now or datetime.now(UTC), root)
    return _probe(args, store, make_reader or _real_reader, now or datetime.now(UTC), root)
