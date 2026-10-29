"""Reutilização curta de HTTP XML idêntico, sem compartilhar parsing ou escopo."""

from __future__ import annotations

import gzip
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from buscador_midias.net.fetcher import FetchResult


class DocumentMemo:
    """Cache limitado e single-flight para leituras simultâneas da mesma requisição."""

    def __init__(self, *, max_bytes: int = 32 * 1024 * 1024, ttl: float = 30) -> None:
        """Limita corpo comprimido, número de entradas e tempo de validade."""
        self.max_bytes, self.ttl = max_bytes, ttl
        self.lock = threading.Lock()
        self.entries: OrderedDict[tuple[Any, ...], tuple[float, FetchResult, bytes]] = OrderedDict()
        self.inflight: dict[tuple[Any, ...], threading.Event] = {}
        self.size = 0

    def get(
        self, key: tuple[Any, ...], fetch: Callable[[], FetchResult]
    ) -> tuple[FetchResult, bool]:
        """Não memoriza falhas/304; o chamador mantém validações e budgets de parsing."""
        while True:
            with self.lock:
                now = time.monotonic()
                for old_key, entry in list(self.entries.items()):
                    if entry[0] <= now:
                        self.size -= len(self.entries.pop(old_key)[2])
                if key in self.entries:
                    entry = self.entries.pop(key)
                    self.entries[key] = entry
                    return replace(entry[1], content=gzip.decompress(entry[2])), True
                event = self.inflight.get(key)
                if event is None:
                    event = self.inflight[key] = threading.Event()
                    break
            if not event.wait(10):
                return fetch(), False
        try:
            response = fetch()
            if response.ok and 0 < len(response.content) <= 4 * 1024 * 1024:
                packed = gzip.compress(response.content, compresslevel=1)
                if len(packed) <= self.max_bytes:
                    with self.lock:
                        while self.entries and (
                            self.size + len(packed) > self.max_bytes or len(self.entries) >= 128
                        ):
                            self.size -= len(self.entries.popitem(last=False)[1][2])
                        self.entries[key] = (
                            time.monotonic() + self.ttl,
                            replace(response, content=b""),
                            packed,
                        )
                        self.size += len(packed)
            return response, False
        finally:
            with self.lock:
                self.inflight.pop(key).set()
