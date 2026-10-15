"""Sessão de rotulagem: lê os lotes cegos e grava o veredito de cada item direto no CSV.

A tela de rotulagem usa esta classe no lugar de a pessoa editar o CSV à mão. O arquivo continua
sendo a única fonte da verdade, então ``labels score`` funciona igual, e quem preferir preencher
no Excel pode misturar os dois jeitos. Só os campos **cegos** passam por aqui: o grupo, os temas e
as entidades do modelo ficam em ``chave.csv``, que esta classe nunca abre.
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from buscador_midias.labeling.csvio import read_rows
from buscador_midias.labeling.verdicts import CSV_WORDS, UNLABELED, parse_verdict

_BATCH_FILE = re.compile(r"^rotulagem-(\d+)\.csv$")
_WRITABLE = frozenset(CSV_WORDS.values())


@dataclass(frozen=True, slots=True)
class LabelItem:
    """Um item a rotular, só com o que a pessoa precisa para julgar.

    Attributes:
        id: Identificador do lote (``L0001``).
        source: Chave da fonte (site).
        url: Endereço da matéria.
        headline: Manchete.
        excerpt: Trecho da matéria (ou o aviso de que só há a manchete).
        verdict: ``sim``, ``nao``, ``incerto`` ou ``""`` (ainda sem rótulo).
    """

    id: str
    source: str
    url: str
    headline: str
    excerpt: str
    verdict: str


class LabelSession:
    """Acesso de leitura e escrita aos lotes ``rotulagem-NN.csv`` de uma pasta.

    Args:
        folder: Pasta com os lotes (a mesma de ``labels export``).
    """

    def __init__(self, folder: Path) -> None:
        """Guarda a pasta e cria o trinco que serializa leituras e gravações."""
        self._folder = folder
        self._lock = threading.Lock()

    def items(self) -> list[LabelItem]:
        """Todos os itens dos lotes, na ordem dos arquivos (já embaralhados na exportação)."""
        with self._lock:
            return [self._item(row) for path in self._batches() for row in read_rows(path)]

    def set_verdict(self, label_id: str, verdict: str) -> None:
        """Grava (ou limpa, com ``""``) o veredito de um item no CSV do lote a que ele pertence.

        A troca do arquivo é atômica (escreve num temporário e renomeia): uma queda no meio não
        deixa CSV pela metade. As demais colunas e linhas ficam como estavam.

        Args:
            label_id: Identificador do item (``L0001``).
            verdict: ``sim``, ``nao``, ``incerto`` ou ``""`` para limpar.

        Raises:
            ValueError: Se o veredito não for um dos permitidos.
            KeyError: Se nenhum lote tiver o identificador.
        """
        if verdict not in _WRITABLE:
            raise ValueError(f"veredito inválido: {verdict!r}")
        with self._lock:
            for path in self._batches():
                rows = read_rows(path)
                match = next((row for row in rows if row.get("id") == label_id), None)
                if match is None:
                    continue
                match["veredito"] = verdict
                self._write(path, rows)
                return
        raise KeyError(label_id)

    def _batches(self) -> list[Path]:
        """Arquivos de lote em ordem numérica."""
        numbered = []
        for path in self._folder.glob("rotulagem-*.csv"):
            match = _BATCH_FILE.match(path.name)
            if match:
                numbered.append((int(match.group(1)), path))
        return [path for _, path in sorted(numbered)]

    @staticmethod
    def _item(row: dict[str, str]) -> LabelItem:
        """Item a partir de uma linha do CSV, descartando qualquer coluna fora do conjunto cego."""
        verdict = parse_verdict(row.get("veredito", ""))
        return LabelItem(
            id=row.get("id", ""),
            source=row.get("fonte", ""),
            url=row.get("url", ""),
            headline=row.get("manchete", ""),
            excerpt=row.get("trecho", ""),
            verdict="" if verdict == UNLABELED else CSV_WORDS[verdict],
        )

    @staticmethod
    def _write(path: Path, rows: list[dict[str, str]]) -> None:
        """Regrava o CSV (UTF-8 com BOM, para o Excel abrir certo) trocando o arquivo todo."""
        fieldnames = list(rows[0]) if rows else []
        descriptor, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=".rotulagem-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
