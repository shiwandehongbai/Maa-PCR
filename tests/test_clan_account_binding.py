"""Only a live Owner lease can reuse an actually observed clan account."""
from types import SimpleNamespace as NS

import pytest

from pcr_maa.clan_battle_native import NativeClanBattle, PROFILE_NODE
from pcr_maa.guide_trial_native import NativeGuideTrial


def bound_port(tmp_path, monkeypatch, *, certified=True):
    owner = NS(key=(object(), object(), 1), revoked=False)
    def source():
        if owner.revoked:
            raise RuntimeError('Owner source changed or revoked')
        return owner.key
    tasker = NS(observation_source=source) if certified else NS()
    port = NativeClanBattle(tasker, tmp_path)
    calls = []
    monkeypatch.setattr(port, 'run', lambda name, **kwargs: calls.append(name) or {'profile': True})
    monkeypatch.setattr(port, 'wait', lambda *args, **kwargs: None)
    monkeypatch.setattr('pcr_maa.clan_battle_native.profile_player_id', lambda receipt: 123)
    return port, owner, calls


def test_three_slots_share_one_actual_profile_read_with_same_live_owner(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    for slot in range(1, 4):
        plan = dict(account_id='123', confirmed_for=str(slot))
        port._verify_account(plan)
        assert port.account_binding_valid(plan)
    assert calls == ['LabyClientOpenProfileOnce', PROFILE_NODE, 'LabyClientProfileBackOnce']
    assert port.account_receipt == {'profile': True}


def test_reconnect_or_new_owner_requires_a_new_real_profile_read(tmp_path, monkeypatch):
    port, owner, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    owner.key = (object(), object(), 2)
    port._verify_account(dict(account_id='123'))
    assert calls.count(PROFILE_NODE) == 2


def test_account_change_rechecks_and_rejects_old_identity(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    with pytest.raises(RuntimeError, match='account_mismatch'):
        port._verify_account(dict(account_id='456'))
    assert calls.count(PROFILE_NODE) == 2 and port.account is None and port._account_binding is None


def test_plan_or_old_gui_account_cache_does_not_create_a_live_binding(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port.account = '123'
    port.account_receipt = {'cached': True}
    port._verify_account(dict(account_id='123'))
    assert calls.count(PROFILE_NODE) == 1 and port.account_receipt == {'profile': True}


def test_source_revocation_stops_before_profile_navigation(tmp_path, monkeypatch):
    port, owner, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    before = list(calls)
    owner.revoked = True
    with pytest.raises(RuntimeError, match='source changed or revoked'):
        port._verify_account(dict(account_id='123'))
    assert calls == before and port.account is None and port._account_binding is None


def test_source_change_during_actual_profile_read_never_binds_identity(tmp_path, monkeypatch):
    port, owner, calls = bound_port(tmp_path, monkeypatch)
    def read(receipt):
        owner.key = (object(), object(), 2)
        return 123
    monkeypatch.setattr('pcr_maa.clan_battle_native.profile_player_id', read)
    with pytest.raises(RuntimeError, match='account_binding_changed'):
        port._verify_account(dict(account_id='123'))
    assert calls.count(PROFILE_NODE) == 1 and port.account is None and port._account_binding is None


def test_uncertified_sdk_views_cannot_reuse_a_profile_identity(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch, certified=False)
    port._verify_account(dict(account_id='123'))
    port._verify_account(dict(account_id='123'))
    assert calls.count(PROFILE_NODE) == 2 and port._account_binding is None


def test_unknown_native_operation_invalidates_the_verified_account(tmp_path, monkeypatch):
    port, _, _ = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    monkeypatch.delattr(port, 'run')
    def fail(*args, **kwargs):
        raise RuntimeError('page_unknown')
    monkeypatch.setattr(NativeGuideTrial, 'run', fail)
    with pytest.raises(RuntimeError, match='page_unknown'):
        port.run('CBPage_lobby')
    assert port.account is None and port._account_binding is None


def test_explicit_account_switch_invalidation_forces_readback(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    port.invalidate_account_binding()
    port._verify_account(dict(account_id='123'))
    assert calls.count(PROFILE_NODE) == 2


def read_optional_policy(port, monkeypatch, value=False, *, during_read=None, found=True):
    port.inheritance_page = NS(ub_auto_clear=True)
    reads = []
    def choose(names):
        assert names == ['CBSetResetRead']
        reads.append(names)
        if during_read:
            during_read()
        detail = dict(auto_release_on_ub=value, same_frame=True, frame_pixel_sha256='a'*64)
        port.last_receipt = dict(native_ok=True, nodes=[dict(recognition=dict(best=dict(detail=detail)))])
        return 'CBSetResetRead' if found else 'GTUnknown'
    monkeypatch.setattr(port, 'choose', choose)
    return port._observe_opening_control_policy(), reads


def test_account_verification_never_reads_optional_settings_or_navigates_inheritance(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port.inheritance_page = NS(ub_auto_clear=True)
    monkeypatch.setattr(port, '_observe_opening_control_policy', lambda: pytest.fail('No automatic settings lookup'))
    for slot in range(1, 4):
        port._verify_account(dict(account_id='123', confirmed_for=str(slot)))
        assert port.freeze_opening_control_policy(dict(account_id='123')) is None
    assert calls == ['LabyClientOpenProfileOnce', PROFILE_NODE, 'LabyClientProfileBackOnce']
    assert port._opening_control_binding is None and port._opening_control_policy is None


@pytest.mark.parametrize('value', [False, True, None])
def test_optional_current_page_read_keeps_real_policy_without_navigation(tmp_path, monkeypatch, value):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    observed, reads = read_optional_policy(port, monkeypatch, value)
    for _ in range(3):
        port._verify_account(dict(account_id='123'))
    policy = port.freeze_opening_control_policy(dict(account_id='123'))
    assert reads == [['CBSetResetRead']]
    assert calls == ['LabyClientOpenProfileOnce', PROFILE_NODE, 'LabyClientProfileBackOnce']
    if value is None:
        assert observed is None and policy is None
    else:
        assert observed['auto_release_on_ub'] is value and policy['account_id'] == '123'
        assert policy['source'] == 'owner_account_bound_settings_readback'
        policy['native_evidence'].clear()
        assert port.freeze_opening_control_policy(dict(account_id='123'))['native_evidence']


def test_unknown_optional_current_page_never_opens_or_closes_settings(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    observed, reads = read_optional_policy(port, monkeypatch, found=False)
    assert observed is None and reads == [['CBSetResetRead']]
    assert port.freeze_opening_control_policy(dict(account_id='123')) is None
    assert calls == ['LabyClientOpenProfileOnce', PROFILE_NODE, 'LabyClientProfileBackOnce']


def test_unbound_optional_policy_does_not_read_any_page(tmp_path, monkeypatch):
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port.inheritance_page = NS(ub_auto_clear=True)
    monkeypatch.setattr(port, 'choose', lambda _: pytest.fail('An actual account binding is required first'))
    assert port._observe_opening_control_policy() is None and calls == []


def test_settings_evidence_from_a_previous_connection_cannot_authorize_opening(tmp_path, monkeypatch):
    port, owner, _ = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    read_optional_policy(port, monkeypatch)
    assert port.freeze_opening_control_policy(dict(account_id='123'))['auto_release_on_ub'] is False
    owner.key = (object(), object(), 2)
    assert port.freeze_opening_control_policy(dict(account_id='123')) is None
    assert port._opening_control_policy is None and port.account is None


def test_source_change_during_optional_read_cannot_bind_settings(tmp_path, monkeypatch):
    port, owner, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    observed, _ = read_optional_policy(port, monkeypatch,
        during_read=lambda: setattr(owner, 'key', (object(), object(), 2)))
    assert observed is None and port._opening_control_policy is None and port.account is None
    assert calls == ['LabyClientOpenProfileOnce', PROFILE_NODE, 'LabyClientProfileBackOnce']


def test_revoked_owner_cannot_reuse_settings_evidence(tmp_path, monkeypatch):
    port, owner, _ = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    read_optional_policy(port, monkeypatch)
    owner.revoked = True
    with pytest.raises(RuntimeError, match='source changed or revoked'):
        port.freeze_opening_control_policy(dict(account_id='123'))
    assert port._opening_control_policy is None and port._opening_control_binding is None


@pytest.mark.parametrize('damage', ['account', 'binding', 'frame', 'evidence'])
def test_unbound_or_incomplete_settings_observation_cannot_become_opening_proof(tmp_path, monkeypatch, damage):
    port, _, _ = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    read_optional_policy(port, monkeypatch)
    if damage == 'account':
        port._opening_control_policy['account_id'] = '456'
    elif damage == 'binding':
        port._opening_control_binding = ('123', (object(),))
    elif damage == 'frame':
        port._opening_control_policy['frame_pixel_sha256'] = None
    else:
        port._opening_control_policy['native_evidence'] = {}
    assert port.freeze_opening_control_policy(dict(account_id='123')) is None


def test_disconnect_invalidates_identity_and_optional_policy(tmp_path, monkeypatch):
    from pcr_maa.clan_battle_recovery import ClanDisconnected
    port, _, _ = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    read_optional_policy(port, monkeypatch)
    monkeypatch.delattr(port, 'run')
    port.battle_start = 'not_sent'
    monkeypatch.setattr(NativeGuideTrial, 'run', lambda *a, **kw: dict(nodes=[dict(name='LabyDisconnect6002Observed')]))
    with pytest.raises(ClanDisconnected):
        port.run('CBPage_lobby')
    assert port.account is None and port._account_binding is None and port._opening_control_policy is None


def test_unknown_optional_policy_does_not_block_matched_inherited_controls(tmp_path, monkeypatch):
    from pcr_maa import clan_battle
    from test_clan_battle import Controls
    port, _, calls = bound_port(tmp_path, monkeypatch)
    port._verify_account(dict(account_id='123'))
    assert port.freeze_opening_control_policy(dict(account_id='123')) is None
    ids = [100101, 100102, 100103, 100104, 100105]
    plan = dict(unit_ids=ids, auto=True, set_by_unit={str(uid): True for uid in ids}, switches=[])
    controls = Controls(plan)
    controls.calibration = dict(version=2, auto_battle=dict(speeds=[1, 2, 4]))
    controls.opening_paused, controls.battle_speed = True, 2
    controls.state.update(paused=True, remaining_seconds=70, same_frame=True, battle_speed=2,
        zero_damage_verified=False, zero_tp_verified=False)
    result = clan_battle.execute_switches(controls, plan)
    assert result['settings_verified'] is True and result['actions'] == [] and controls.actions == []
    assert calls == ['LabyClientOpenProfileOnce', PROFILE_NODE, 'LabyClientProfileBackOnce']
