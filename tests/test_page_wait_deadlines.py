"""The shared page waiter must reject late reads and restore its caller's budget."""
from types import SimpleNamespace as NS

import pytest

from pcr_maa import native, guide_trial_native
from test_native_stop_bounds import Clock


@pytest.mark.parametrize('parent,seconds,read_seconds', [(float('inf'), .5, .6), (.4, 2., .6)])
def test_shared_clan_page_wait_rejects_late_success(monkeypatch, parent, seconds, read_seconds):
    clock = Clock()
    monkeypatch.setattr(guide_trial_native, 'time', clock)
    deadlines = []

    def choose(names):
        deadlines.append(native.RUN_DEADLINE.get())
        clock.now += read_seconds
        return 'CBPage_formation'

    port = guide_trial_native.NativeGuideTrial.__new__(guide_trial_native.NativeGuideTrial)
    port.choose = choose
    token = native.RUN_DEADLINE.set(parent)
    try:
        with pytest.raises(RuntimeError, match='^guide_trial_page_timeout$'):
            port.wait(['CBPage_formation'], seconds=seconds)
        assert deadlines == [min(parent, seconds)]
        assert native.RUN_DEADLINE.get() == parent
    finally:
        native.RUN_DEADLINE.reset(token)


def test_shared_page_wait_preserves_early_success_and_unknown_polling(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(guide_trial_native, 'time', clock)
    port = guide_trial_native.NativeGuideTrial.__new__(guide_trial_native.NativeGuideTrial)
    pages = iter(['GTUnknown', 'CBPage_formation'])
    port.choose = lambda names: next(pages)
    assert port.wait(['CBPage_formation'], seconds=1) == 'CBPage_formation'
    assert native.RUN_DEADLINE.get() == float('inf')


def test_clock_crossing_deadline_never_requests_negative_sleep(monkeypatch):
    times = iter((0., 0., .9, 1.1, 1.1))
    delays = []
    monkeypatch.setattr(guide_trial_native, 'time', NS(monotonic=lambda: next(times), sleep=delays.append))
    port = guide_trial_native.NativeGuideTrial.__new__(guide_trial_native.NativeGuideTrial)
    port.choose = lambda names: 'GTUnknown'
    with pytest.raises(RuntimeError, match='^guide_trial_page_timeout$'):
        port.wait(['CBPage_formation'], seconds=1)
    assert delays == [0] and native.RUN_DEADLINE.get() == float('inf')
