"""Tests for asynchronous request coordination primitives."""

from enum import Enum, auto

from chemfit_dashboard.workers import GenerationTracker


class _Request(Enum):
    LOAD = auto()
    SEARCH = auto()


def test_generation_tracker_keeps_request_families_independent():
    generations = GenerationTracker[_Request]()

    load = generations.advance(_Request.LOAD)
    search = generations.advance(_Request.SEARCH)

    assert generations.is_current(_Request.LOAD, load)
    assert generations.is_current(_Request.SEARCH, search)
    generations.invalidate(_Request.LOAD)
    assert not generations.is_current(_Request.LOAD, load)
    assert generations.is_current(_Request.SEARCH, search)


def test_generation_tracker_can_invalidate_all_observed_requests():
    generations = GenerationTracker[_Request]()
    tokens = {
        request: generations.advance(request)
        for request in (_Request.LOAD, _Request.SEARCH)
    }

    generations.invalidate_all()

    assert all(
        not generations.is_current(request, token) for request, token in tokens.items()
    )
