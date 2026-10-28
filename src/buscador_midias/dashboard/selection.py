"""Leitura do pacote estático de seleção, sem conexão ao corpus ou acesso aos veículos."""

from __future__ import annotations

import csv
import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

DOWNLOADS = {
    "plano-frequente.json": "application/json",
    "plano-periodico.json": "application/json",
    "plano-referencia-comparacao.json": "application/json",
    "classificacao-mecanismos.xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "comparacao.md": "text/markdown; charset=utf-8",
    "comparacao.json": "application/json",
    "manifesto.json": "application/json",
}
CATEGORIES = frozenset(("all", "frequente", "periodico", "excluir", "inconclusivo"))


class SelectionView:
    """Retrato em memória atualizado somente quando o manifesto local muda."""

    def __init__(self, directory: Path) -> None:
        """Define a pasta permitida e a trava das consultas."""
        self.directory = directory
        self.lock = threading.RLock()
        self.stamp: tuple[int, int] | None = None
        self.data: dict[str, Any] = {}
        self.by_source: dict[str, list[dict[str, Any]]] = {}

    def _refresh(self) -> None:
        """Carrega apenas colunas usadas pela tela, preservando custos desconhecidos."""
        stat = (self.directory / "manifesto.json").stat()
        stamp = stat.st_mtime_ns, stat.st_size
        if stamp == self.stamp:
            return
        manifest = json.loads((self.directory / "manifesto.json").read_text(encoding="utf-8"))
        catalog = json.loads((self.directory / "fontes.json").read_text(encoding="utf-8"))
        sources = [
            dict(
                key=r["fonte"],
                name=r["nome"],
                country=r["pais"],
                url=r["url"],
                frequent=int(r["frequentes"]),
                periodic=int(r["periodicos"]),
                excluded=int(r["excluidos"]),
                inconclusive=int(r["inconclusivos"]),
                gap=r["lacuna_proveniencia"] == "True",
                partial=r["fonte_parcial"] == "True",
                has_text=r["possui_texto_confirmado"] == "True",
            )
            for r in catalog
        ]
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        with (self.directory / "mecanismos.csv").open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                by_source[row["fonte"]].append(
                    dict(
                        url=row["mecanismo"],
                        category=row["classificacao"],
                        reason=row["explicacao"],
                        kind=row["tipo_reutilizavel"],
                        example=row["exemplo_textual"],
                        audio_example=row["exemplo_audio"],
                        documents=int(row["documentos_processados"])
                        if row["documentos_processados"]
                        else None,
                        texts=int(row["textos_confirmados"]),
                        pending=int(row["documentos_pendentes"]),
                        usable=row["endpoint_reutilizavel"] == "True",
                    )
                )
        self.data = dict(manifest=manifest, sources=sources)
        self.by_source = dict(by_source)
        self.stamp = stamp

    def summary(self) -> dict[str, Any]:
        """Retorna catálogo completo e totais da entrega estática."""
        with self.lock:
            self._refresh()
            return self.data

    def mechanisms(
        self, site: str, category: str, search: str, offset: int, limit: int
    ) -> dict[str, Any]:
        """Filtra e pagina decisões por fonte; nenhum texto é interpretado como HTML."""
        with self.lock:
            self._refresh()
            if site not in {row["key"] for row in self.data["sources"]}:
                raise KeyError(site)
            search = search.casefold()
            values = [
                r
                for r in self.by_source.get(site, [])
                if (category == "all" or r["category"] == category)
                and (not search or search in (r["url"] + " " + r["reason"]).casefold())
            ]
            return dict(
                site=site,
                total=len(values),
                offset=offset,
                limit=limit,
                items=values[offset : offset + limit],
            )
