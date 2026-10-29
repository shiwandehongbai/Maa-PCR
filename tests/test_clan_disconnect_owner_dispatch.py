from types import SimpleNamespace as NS

import pytest

from pcr_maa import native
from pcr_maa.clan_battle_daily_native import NativeClanDailyAdapter
from pcr_maa.clan_battle_native import NativeClanBattle
from pcr_maa.clan_battle_recovery import ClanDisconnected, DISCONNECT
from pcr_maa.guide_trial_native import NativeGuideTrial


@pytest.fixture(params=['simulation', 'formal'])
def port(request, tmp_path):
    tasker = NS(resource=NS(override_pipeline=lambda nodes: True))
    if request.param == 'simulation':
        value = NativeClanBattle(tasker, tmp_path)
    else:
        value = object.__new__(NativeClanDailyAdapter)
        NativeClanBattle.__init__(value, tasker, tmp_path)
        value.pending, value.action = None, None
        value.calibration = {'daily': {'execution': {}}}
    value.checkpoint = lambda: None
    return value


def dispatch(monkeypatch, failure, *, hit=DISCONNECT, probe_fails=False):
    calls = []

    def run(self, node, *, click=False, reset_hits=()):
        calls.append((node, click))
        if node != 'GTChoose':
            raise failure
        if probe_fails:
            raise RuntimeError('probe failed')
        return dict(native_ok=True, tasker_inited=True, task_id=7, nodes=[dict(name=hit)])

    monkeypatch.setattr(NativeGuideTrial, 'run', run)
    return calls


@pytest.mark.parametrize('click,proven_not_sent', [(False, False), (True, False), (True, True)])
def test_failed_dispatch_probes_6002_once_and_never_repeats_input(port, monkeypatch, click, proven_not_sent):
    failure = (native.NativeInputNotSent({'input_execution': 'not_sent'}) if proven_not_sent
        else RuntimeError('original failed'))
    calls = dispatch(monkeypatch, failure)
    with pytest.raises(ClanDisconnected) as caught:
        port.run('CBAction_open_clan' if click else 'CBPage_lobby', click=click)
    assert caught.value.original_error is failure and caught.value.__cause__ is failure
    assert caught.value.replay_forbidden is click
    assert calls == [('CBAction_open_clan' if click else 'CBPage_lobby', click), ('GTChoose', False)]
    assert caught.value.receipt is port.last_receipt and port.battle_start == 'not_sent'


def test_non_disconnect_keeps_original_exception(port, monkeypatch):
    failure = RuntimeError('original failed')
    calls = dispatch(monkeypatch, failure, hit='GTUnknown')
    with pytest.raises(RuntimeError) as caught:
        port.run('CBPage_lobby')
    assert caught.value is failure
    assert calls == [('CBPage_lobby', False), ('GTChoose', False)]


def test_blocked_owner_keeps_original_exception_without_probe(port, monkeypatch):
    failure = RuntimeError('original failed')
    calls = dispatch(monkeypatch, failure)
    port.checkpoint = lambda: (_ for _ in ()).throw(RuntimeError('owner blocked'))
    with pytest.raises(RuntimeError) as caught:
        port.run('CBPage_lobby')
    assert caught.value is failure and calls == [('CBPage_lobby', False)]


def test_probe_failure_does_not_recurse_or_replace_original(port, monkeypatch):
    failure = RuntimeError('original failed')
    calls = dispatch(monkeypatch, failure, probe_fails=True)
    with pytest.raises(RuntimeError) as caught:
        port.run('CBPage_lobby')
    assert caught.value is failure
    assert calls == [('CBPage_lobby', False), ('GTChoose', False)]


def test_existing_typed_disconnect_is_preserved_without_probe(port, monkeypatch):
    failure = ClanDisconnected({'task_id': 2}, 'not_sent')
    calls = dispatch(monkeypatch, failure)
    with pytest.raises(ClanDisconnected) as caught:
        port.run('CBPage_lobby')
    assert caught.value is failure and calls == [('CBPage_lobby', False)]


def test_start_proven_not_sent_keeps_original_receipt_for_journal(port, monkeypatch):
    evidence = {'input_execution': 'not_sent'}
    failure = native.NativeInputNotSent(evidence)
    calls = dispatch(monkeypatch, failure)
    if isinstance(port, NativeClanDailyAdapter):
        port.pending, port.action = {'intent_id': 'reserved'}, {'kind': 'regular'}
    with pytest.raises(native.NativeInputNotSent) as caught:
        port.run('CBStart', click=True)
    assert caught.value is failure and failure.receipt is evidence
    assert evidence['clan_battle_phase'] == ('regular_start' if isinstance(port, NativeClanDailyAdapter) else 'simulation_start')
    if isinstance(port, NativeClanDailyAdapter):
        assert evidence['daily_intent_id'] == 'reserved'
    assert port.battle_start == 'not_sent' and calls == [('CBStart', True)]


def test_unknown_start_probe_keeps_attempt_unknown_and_forbids_replay(port, monkeypatch):
    failure = RuntimeError('start outcome unknown')
    calls = dispatch(monkeypatch, failure)
    if isinstance(port, NativeClanDailyAdapter):
        port.pending, port.action = {'intent_id': 'reserved'}, {'kind': 'regular'}
    with pytest.raises(ClanDisconnected) as caught:
        port.run('CBStart', click=True)
    assert caught.value.original_error is failure and caught.value.replay_forbidden is True
    assert port.started and port.battle_start == 'unknown'
    assert calls == [('CBStart', True), ('GTChoose', False)]
