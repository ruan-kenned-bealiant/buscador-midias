"""Disjuntor do provedor: uma falha isolada não pode parar uma etapa inteira."""

import pytest

from buscador_midias.ingestion.flow import ProviderBreaker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def breaker(clock):
    return ProviderBreaker(threshold=5, window=60, pause=45, clock=clock)


def test_isolated_failure_does_not_open_the_stage(breaker):
    assert breaker.failure("context", "HTTP 502") is False
    assert breaker.paused("context") is None


def test_failures_below_the_threshold_do_not_open(breaker):
    for _ in range(4):
        assert breaker.failure("context", "HTTP 502") is False
    assert breaker.paused("context") is None


def test_threshold_failures_within_the_window_open_the_stage(breaker):
    for _ in range(4):
        breaker.failure("context", "HTTP 502")
    assert breaker.failure("context", "HTTP 503") is True
    assert breaker.paused("context") == "HTTP 503"


def test_failures_outside_the_window_are_forgotten(breaker, clock):
    for _ in range(4):
        breaker.failure("context", "HTTP 502")
    clock.now += 61
    assert breaker.failure("context", "HTTP 502") is False
    assert breaker.paused("context") is None


def test_stage_reopens_for_work_after_the_pause(breaker, clock):
    for _ in range(5):
        breaker.failure("context", "HTTP 502")
    clock.now += 44
    assert breaker.paused("context") == "HTTP 502"
    clock.now += 2
    assert breaker.paused("context") is None


def test_failures_in_flight_during_the_pause_do_not_extend_it(breaker, clock):
    for _ in range(5):
        breaker.failure("context", "HTTP 502")
    clock.now += 30
    for _ in range(10):
        assert breaker.failure("context", "HTTP 502") is False
    clock.now += 16
    assert breaker.paused("context") is None


def test_after_the_pause_the_count_starts_again(breaker, clock):
    for _ in range(5):
        breaker.failure("context", "HTTP 502")
    clock.now += 46
    for _ in range(4):
        assert breaker.failure("context", "HTTP 502") is False
    assert breaker.paused("context") is None


def test_stages_are_independent(breaker):
    for _ in range(5):
        breaker.failure("context", "HTTP 502")
    assert breaker.paused("context") is not None
    assert breaker.paused("evidence") is None
    assert breaker.failure("evidence", "HTTP 502") is False


def test_default_policy_is_five_failures_in_a_minute_with_a_one_minute_pause():
    breaker = ProviderBreaker()
    assert (breaker.threshold, breaker.window, breaker.pause) == (5, 60, 60)
