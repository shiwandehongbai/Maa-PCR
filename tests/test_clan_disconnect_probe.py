import pytest

from pcr_maa import native
from pcr_maa.clan_battle_recovery import ClanDisconnected, observe_disconnect_after_failure


class ProbePort:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []
        self.battle_start = 'unknown'
        self.last_receipt = {'task_id': 5}

    def checkpoint(self):
        self.calls.append('checkpoint')

    def choose(self, names, *, timeout):
        self.calls.append((tuple(names), timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_detects_current_readonly_disconnect_without_retrying_input():
    port = ProbePort(None)
    disconnect = ClanDisconnected(port.last_receipt, port.battle_start)
    port.outcome = disconnect
    cause = RuntimeError('input outcome unknown')
    assert observe_disconnect_after_failure(port, cause, input_attempted=True) is disconnect
    assert disconnect.original_error is cause and disconnect.replay_forbidden is True
    assert port.calls == ['checkpoint', ((), 1000)]
    assert port.battle_start == 'unknown'


def test_non_disconnect_is_only_an_observation():
    port = ProbePort('GTUnknown')
    assert observe_disconnect_after_failure(port, RuntimeError('page unknown')) is None
    assert port.calls == ['checkpoint', ((), 1000)]


def test_blocked_owner_stops_without_recovery_or_replay():
    failure = RuntimeError('owner_blocked')
    port = ProbePort(failure)
    assert observe_disconnect_after_failure(port, RuntimeError('original')) is None
    assert port.calls == ['checkpoint', ((), 1000)]


def test_proven_not_sent_is_not_mistaken_for_unknown_input():
    port = ProbePort(None)
    disconnect = ClanDisconnected(port.last_receipt, port.battle_start)
    port.outcome = disconnect
    cause = native.NativeInputNotSent({'input_execution': 'not_sent'})
    assert observe_disconnect_after_failure(port, cause, input_attempted=True) is disconnect
    assert disconnect.original_error is cause and disconnect.replay_forbidden is False


def test_nested_failure_during_probe_does_not_recurse_or_retry():
    port = ProbePort(None)
    original = RuntimeError('original')

    def choose(names, *, timeout):
        port.calls.append((tuple(names), timeout))
        assert observe_disconnect_after_failure(port, RuntimeError('nested')) is None
        raise RuntimeError('nested')

    port.choose = choose
    assert observe_disconnect_after_failure(port, original) is None
    assert port.calls == ['checkpoint', ((), 1000)]
    assert port._clan_disconnect_probe_active is False


def test_stopped_owner_is_not_probed_or_unblocked():
    port = ProbePort('GTUnknown')

    def stopped():
        port.calls.append('checkpoint')
        raise RuntimeError('stopped')

    port.checkpoint = stopped
    assert observe_disconnect_after_failure(port, RuntimeError('original')) is None
    assert port.calls == ['checkpoint'] and port._clan_disconnect_probe_active is False


@pytest.mark.parametrize('cause,input_attempted', [(None, False), (RuntimeError(), 'yes')])
def test_probe_requires_internal_exception_and_boolean_input_flag(cause, input_attempted):
    port = ProbePort('GTUnknown')
    with pytest.raises(ValueError, match='probe_invalid'):
        observe_disconnect_after_failure(port, cause, input_attempted=input_attempted)
    assert not port.calls
