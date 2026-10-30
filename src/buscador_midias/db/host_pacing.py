"""Ritmo por site da rodada de saúde: respiro entre leituras e espera maior depois de um 429.

A rodada lê um link por vez de cada site pequeno, mas sem intervalo isso são vários pedidos por
segundo, e sites pequenos respondem 429. Aqui cada site descansa depois de uma leitura, e um 429
(ou o ``Retry-After`` que o site pediu, com teto) o faz descansar bem mais, inclusive os sites
grandes, que de outro modo leem em paralelo.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: Teto da espera pedida pelo site: um ``Retry-After`` de um dia não pode parar a rodada.
MAX_REST = 300.0


class HostPacing:
    """Quais sites ainda descansam e, portanto, não devem receber leitura nova agora."""

    def __init__(
        self,
        gap: float = 3.0,
        throttle_pause: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``gap``: intervalo entre leituras de um site pequeno; ``throttle_pause``: após um 429."""
        self.gap, self.throttle_pause, self.clock = gap, throttle_pause, clock
        self._until: dict[str, tuple[float, bool]] = {}

    def finished(
        self,
        host: str,
        *,
        big: bool,
        throttled: bool = False,
        retry_after: float | None = None,
    ) -> None:
        """Registra o fim de uma leitura; ``throttled`` quando o site respondeu 429."""
        if throttled:
            rest = min(max(retry_after or 0.0, self.throttle_pause), MAX_REST)
            self._until[host] = (self.clock() + rest, True)
        elif not big and self.gap > 0:
            self._until[host] = (self.clock() + self.gap, False)

    def held(self, big_hosts: set[str], big_slots: int) -> list[str]:
        """Sites em descanso, repetidos tantas vezes quantos slots têm (esgota a vaga do site)."""
        now = self.clock()
        self._until = {h: (t, thr) for h, (t, thr) in self._until.items() if t > now}
        held: list[str] = []
        for host, (_until, throttled) in self._until.items():
            slots = big_slots if host in big_hosts and throttled else 1
            held.extend([host] * slots)
        return held

    def resting(self) -> bool:
        """Se algum site ainda descansa (a rodada não pode se dar por encerrada)."""
        return bool(self.held(set(), 1))
