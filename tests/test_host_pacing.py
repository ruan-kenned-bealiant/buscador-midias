"""Ritmo por host da rodada de saúde: intervalo entre leituras e espera após um 429."""

from __future__ import annotations

from buscador_midias.db.host_pacing import HostPacing


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def pacing(gap: float = 3.0, pause: float = 30.0) -> tuple[HostPacing, Clock]:
    clock = Clock()
    return HostPacing(gap=gap, throttle_pause=pause, clock=clock), clock


def test_a_small_site_rests_between_two_reads():
    held, clock = pacing()
    held.finished("a.example", big=False)

    assert held.held(set(), 3) == ["a.example"]
    clock.now += 2.9
    assert held.held(set(), 3) == ["a.example"]
    clock.now += 0.2
    assert held.held(set(), 3) == []


def test_a_big_site_keeps_its_parallel_reads_without_a_gap():
    held, _ = pacing()
    held.finished("big.example", big=True)

    assert held.held({"big.example"}, 3) == []


def test_a_throttled_site_rests_long_even_when_it_is_big():
    held, clock = pacing()
    held.finished("big.example", big=True, throttled=True)

    # Ocupa todos os slots do site grande, para nenhuma leitura nova sair.
    assert held.held({"big.example"}, 3) == ["big.example"] * 3
    clock.now += 29
    assert held.held({"big.example"}, 3) == ["big.example"] * 3
    clock.now += 2
    assert held.held({"big.example"}, 3) == []


def test_the_site_asked_for_a_longer_wait_and_gets_it():
    held, clock = pacing(pause=30)
    held.finished("a.example", big=False, throttled=True, retry_after=90)

    clock.now += 60
    assert held.held(set(), 3) == ["a.example"]
    clock.now += 31
    assert held.held(set(), 3) == []


def test_the_wait_the_site_asks_for_is_capped_so_one_host_cannot_stall_the_round():
    held, clock = pacing(pause=30)
    held.finished("a.example", big=False, throttled=True, retry_after=86_400)

    clock.now += 301
    assert held.held(set(), 3) == []
