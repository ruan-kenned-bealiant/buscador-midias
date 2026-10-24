"""Plano explícito de atualizações do g1, usando somente filhos do índice oficial."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from buscador_midias.catalog.mechanisms.base import CollectionPlan, Parameter
from buscador_midias.catalog.mechanisms.parsing import Document
from buscador_midias.catalog.mechanisms.sitemap_index import SitemapIndexMechanism

_CHILD = re.compile(
    r"https://g1\.globo\.com/sitemap/g1/(?P<year>\d{4})/(?P<month>\d{2})/"
    r"(?P<day>\d{2})(?:_\d+)?\.xml(?:\.gz)?$"
)


class G1DailyMechanism(SitemapIndexMechanism):
    """Seleciona partições diárias conhecidas; publicação continua sendo confirmada na notícia."""

    id = "g1_daily"
    label = "g1 · atualizações por dia"
    description = "Lê o índice oficial e seleciona somente os XMLs da janela e sua sobreposição."
    parameters = SitemapIndexMechanism.parameters + (
        Parameter(
            "updates_overlap_days",
            "Sobreposição do índice diário (dias)",
            1,
            "Inclui dias vizinhos; não usa lastmod como publicação. 0 limita às datas da janela.",
            0,
            2,
        ),
        Parameter(
            "updates_timezone",
            "Fuso das partições do índice",
            "UTC",
            "Define o calendário dos arquivos, independente do fuso da janela do usuário.",
            choices=(("UTC", "UTC"), ("America/Sao_Paulo", "Brasília")),
        ),
    )

    def read(self, body: bytes, url: str, settings: dict[str, Any]) -> Document:
        """Impede aplicar a estratégia específica a outro portal ou ao índice de vídeos."""
        if url != "https://g1.globo.com/sitemap/g1/sitemap.xml" and not _CHILD.fullmatch(url):
            raise ValueError("Use o índice oficial de notícias do g1 ou seus arquivos diários.")
        return super().read(body, url, settings)

    def plan_collection(
        self, document: Document, since: datetime, until: datetime, settings: dict[str, Any]
    ) -> CollectionPlan:
        """Mantém todas as páginas dos dias escolhidos e sinaliza mudanças de estrutura."""
        if since.tzinfo is None or until.tzinfo is None or since > until:
            raise ValueError("Janela inválida para o índice diário.")
        timezone = ZoneInfo(settings.get("updates_timezone", "UTC"))
        overlap = timedelta(days=settings.get("updates_overlap_days", 1))
        first = since.astimezone(timezone).date() - overlap
        last = until.astimezone(timezone).date() + overlap
        selected, unknown, skipped = [], [], 0
        for child in dict.fromkeys(document["children"]):
            match = _CHILD.fullmatch(child)
            try:
                stamp = date(**{k: int(v) for k, v in match.groupdict().items()}) if match else None
            except ValueError:
                stamp = None
            if stamp is None:
                unknown.append(child)
            elif first <= stamp <= last:
                selected.append((stamp, child))
            else:
                skipped += 1
        selected.sort(reverse=True)
        return CollectionPlan(
            tuple(u for _, u in selected), skipped, tuple(unknown), "g1_daily_index"
        )
