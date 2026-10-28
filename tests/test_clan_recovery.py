from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from pcr_maa import native
from pcr_maa.labyrinth.session import reconnect_pipeline
from pcr_maa.clan_battle_recovery import (
    AFTER_TITLE_PAGES, DISCONNECT, RECOVERY_PAGES, ClanDisconnected,
    observe_disconnect_after_failure, recover_disconnect,
)

PLAN = dict(account_id='123456789')


def receipt(node, *, click=False, recognition=None):
    if node == DISCONNECT and recognition is None:
        texts = ['错误提示', '错误代码：6002', '连接中断。', '回到标题界面。', '返回标题']
        rules, recognition = reconnect_pipeline(), []
        for name, text in zip(rules[DISCONNECT]['all_of'], texts):
            box = list(rules[name]['roi'])
            word = dict(text=text, score=.99, box=box)
            recognition.append(dict(name=name, algorithm='OCR', box=box, detail=dict(filtered=[word])))
    return dict(native_ok=True, tasker_inited=True, task_id=1, nodes=[dict(
        name=node, completed=True, recognition=recognition,
        action=dict(name=node, success=True, raw={}) if click or node == DISCONNECT else None)])


def choice(node, choices, hit):
    rows = [dict(name=name, algorithm='And', box=[1, 1, 2, 2] if name == hit else None)
            for name in choices[:choices.index(hit) + 1]]
    return receipt(node, recognition=rows)


def title(account_id):
    word = dict(text='ID:' + account_id, score=.99, box=[1065, 683, 200, 30])
    return receipt('LabyClientTitleObserved', recognition=[dict(
        name='LabyClientTitlePlayerId', algorithm='OCR', detail=dict(filtered=[word]))])


class Port:
    def __init__(self, *, start='not_sent', pending=None):
        self.calls, self.source, self.revoked, self.cancelled, self.native_failed = [], object(), False, False, False
        self.battle_start, self.pending, self.started = start, pending, start != 'not_sent'
        self.last_receipt = receipt(DISCONNECT)
        self.tasker = NS(resource=NS(override_pipeline=lambda nodes: True,
            get_node_data=lambda name: dict(action=dict(type='DoNothing', param={}))))
        self.account = PLAN['account_id']
        self._account_binding, self._opening_control_policy = object(), {'old': True}
        self.account_receipt, self.authorizations = {'old_account': True}, ['old_approval']
        self.page = NS(current='battle', expected='result', bound={'old': True})
        self.opening_paused, self.opening_valid = True, True
        self.last_bosses, self.stocks, self.boss_history = {3: 'old'}, ['old'], {3: 'old'}
        self.inheritance_recovered = True
        self.actual_title, self.actual_profile = PLAN['account_id'], PLAN['account_id']
        self.entry_page, self.after_pages = 'LabyClientTitleObserved', ['LabyClientNoticeObserved'] * 2
        self.fail_at, self.change_source_at, self.mutate_receipt = None, None, None
        self.journal = [{'state': 'in_flight'}] if pending else []

    def checkpoint(self):
        if self.cancelled:
            raise RuntimeError('stopped')
        if self.native_failed:
            raise RuntimeError('native_tasker_blocked')
        if self.revoked:
            raise RuntimeError('owner_revoked')

    def _account_source(self):
        self.checkpoint()
        return self.source

    def invalidate_account_binding(self):
        self._account_binding = self._opening_control_policy = None
        self.account = self.account_receipt = None
        self.opening_valid = False

    def invalidate_consumption_authorizations(self):
        self.authorizations = []

    def run(self, node, *, click=False):
        self.calls.append((node, click))
        if node == self.fail_at:
            self.native_failed = True
            raise TimeoutError('native_timeout')
        if node == self.change_source_at:
            self.source = object()
        if node == 'LabyGameSessionRecoveryObserved':
            result = choice(node, RECOVERY_PAGES, self.entry_page)
        elif node == 'LabyGameSessionAfterTitleObserved':
            result = choice(node, AFTER_TITLE_PAGES, self.after_pages.pop(0))
        elif node == 'LabyClientTitleObserved':
            result = title(self.actual_title)
        else:
            result = receipt(node, click=click)
        if self.mutate_receipt:
            self.mutate_receipt(node, result)
        self.last_receipt = result
        return result

    def _verify_account(self, plan):
        self.calls.append(('verify_account', False))
        if self.actual_profile != plan['account_id']:
            raise RuntimeError('clan_battle_account_mismatch')
        self.account, self._account_binding = self.actual_profile, object()
        self.account_receipt = {'fresh_profile': True}

    def account_binding_valid(self, plan):
        return self._account_binding is not None and self.account == plan['account_id']


def recover(port, *, pending=False):
    return recover_disconnect(port, PLAN, ClanDisconnected(port.last_receipt, port.battle_start), pending=pending)


def test_safe_recovery_reuses_owner_and_invalidates_all_stale_bindings():
    port = Port()
    owner, old_binding = port.source, port._account_binding
    result = recover(port)
    assert result['can_continue'] is True and result['account_id'] == PLAN['account_id']
    assert port.source is owner and port._account_binding is not old_binding
    assert port.account_receipt == {'fresh_profile': True}
    assert not port.authorizations and not port._opening_control_policy and not port.opening_valid
    assert port.page.current is port.page.expected is port.page.bound is None
    assert not port.last_bosses and not port.stocks and not port.boss_history
    assert not port.inheritance_recovered and not port.opening_paused
    assert [node for node, click in port.calls if click] == [
        'LabyDisconnectReturnTitleOnce', 'LabyClientTitleStartOnce', 'LabyClientNoticeCloseOnce',
        'LabyClientHomeToAdventure', 'LabyClientOpenMainMenuOnce']
    assert all('CBStart' not in node and 'StopApp' not in node and 'StartApp' not in node for node, _ in port.calls)


@pytest.mark.parametrize('start,pending', [('sent', None), ('unknown', None), ('previously_sent', None),
    ('not_sent', {'intent_id': 'reserved'})])
def test_pending_or_possible_battle_is_preserved_and_never_resumed(start, pending):
    port = Port(start=start, pending=pending)
    journal = deepcopy(port.journal)
    result = recover(port)
    assert result['can_continue'] is False
    assert port.battle_start == start and port.pending is pending and port.journal == journal
    assert not port.calls


def test_explicit_pending_cannot_be_hidden_by_missing_port_field():
    port = Port()
    assert recover(port, pending=True)['can_continue'] is False
    assert not port.calls


def test_native_proven_not_sent_does_not_reset_prior_start_attempt():
    port = Port()
    port.started = True
    assert recover(port)['can_continue'] is False and port.started is True


@pytest.mark.parametrize('replay_forbidden', [True, False])
def test_failed_input_outcome_controls_whether_prebattle_work_can_continue(replay_forbidden):
    port = Port()
    error = ClanDisconnected(port.last_receipt, port.battle_start)
    error.replay_forbidden = replay_forbidden
    result = recover_disconnect(port, PLAN, error)
    assert result['can_continue'] is not replay_forbidden
    assert not any(node == 'CBStart' for node, _ in port.calls)


@pytest.mark.parametrize('proven_not_sent', [True, False])
def test_probe_and_recovery_keep_unknown_prior_input_from_being_repeated(proven_not_sent):
    port = Port()
    original = (native.NativeInputNotSent({'input_execution': 'not_sent'}) if proven_not_sent
        else RuntimeError('Native input outcome unknown; no retry'))

    def choose(names, *, timeout):
        assert names == [] and timeout == 1000
        raise ClanDisconnected(port.last_receipt, port.battle_start)

    port.choose = choose
    error = observe_disconnect_after_failure(port, original, input_attempted=True)
    result = recover_disconnect(port, PLAN, error)
    assert result['can_continue'] is proven_not_sent
    assert error.original_error is original
    assert all(node not in ('CBStart', 'CBAction_open_clan') for node, _ in port.calls)


@pytest.mark.parametrize('field', ['actual_title', 'actual_profile'])
def test_wrong_account_stops_and_leaves_binding_invalid(field):
    port = Port()
    setattr(port, field, '987654321')
    with pytest.raises(RuntimeError, match='account_mismatch'):
        recover(port)
    assert port._account_binding is None and not port.authorizations
    if field == 'actual_title':
        assert not any(node == 'LabyClientTitleStartOnce' for node, _ in port.calls)


@pytest.mark.parametrize('field', ['cancelled', 'native_failed', 'revoked'])
def test_stopped_blocked_or_revoked_owner_has_zero_input(field):
    port = Port()
    setattr(port, field, True)
    with pytest.raises(RuntimeError):
        recover(port)
    assert not port.calls


def test_source_change_after_navigation_revokes_without_further_input():
    port = Port()
    port.change_source_at = 'LabyDisconnectReturnTitleOnce'
    with pytest.raises(RuntimeError, match='owner_changed'):
        recover(port)
    assert port.calls == [('LabyDisconnectReturnTitleOnce', True)]
    assert port._account_binding is None and not port.authorizations


def test_timeout_does_not_clear_native_block_or_retry():
    port = Port()
    port.fail_at = 'LabyGameSessionRecoveryObserved'
    with pytest.raises(TimeoutError, match='native_timeout'):
        recover(port)
    assert port.native_failed and port._clan_disconnect_recovery_used
    assert port.calls == [('LabyDisconnectReturnTitleOnce', True), ('LabyGameSessionRecoveryObserved', False)]


def test_recovery_budget_and_owner_deadline_are_preserved(monkeypatch):
    port, clock = Port(), [0.0]
    monkeypatch.setattr('pcr_maa.clan_battle_recovery.time.monotonic', lambda: clock[0])
    port.mutate_receipt = lambda node, result: clock.__setitem__(0, 121.0)
    token = native.RUN_DEADLINE.set(1000.0)
    try:
        with pytest.raises(RuntimeError, match='recovery_timeout'):
            recover(port)
        assert native.RUN_DEADLINE.get() == 1000.0
    finally:
        native.RUN_DEADLINE.reset(token)
    assert port.calls == [('LabyDisconnectReturnTitleOnce', True)]


def test_second_6002_never_gets_second_return_title():
    port = Port()
    recover(port)
    port.last_receipt = receipt(DISCONNECT)
    before = list(port.calls)
    with pytest.raises(RuntimeError, match='already_attempted'):
        recover(port)
    assert port.calls == before


@pytest.mark.parametrize('mutation', ['failed', 'unfinished', 'wrong_node', 'stale', 'wrong_start'])
def test_only_current_successful_6002_receipt_can_enter_recovery(mutation):
    port = Port()
    error = ClanDisconnected(port.last_receipt, port.battle_start)
    if mutation == 'failed': error.receipt['native_ok'] = False
    elif mutation == 'unfinished': error.receipt['nodes'][0]['completed'] = False
    elif mutation == 'wrong_node': error.receipt['nodes'][0]['name'] = 'GTUnknown'
    elif mutation == 'stale': port.last_receipt = deepcopy(port.last_receipt)
    else: error.battle_start = 'sent'
    with pytest.raises(RuntimeError, match='disconnect_unproven'):
        recover_disconnect(port, PLAN, error)
    assert not port.calls


@pytest.mark.parametrize('page', ['LabyDateChangedObserved', DISCONNECT, 'LabyClientDownloadErrorObserved'])
def test_unexpected_login_state_stops_without_guessing_authentication(page):
    port = Port()
    port.after_pages = [page]
    with pytest.raises(RuntimeError):
        recover(port)
    assert not any(node in ('LabyClientNoticeCloseOnce', 'LabyClientHomeToAdventure') for node, _ in port.calls)


def test_direct_home_still_rechecks_real_profile():
    port = Port()
    port.entry_page = 'LabyClientGameHomeObserved'
    assert recover(port)['can_continue'] is True
    assert ('verify_account', False) in port.calls
    assert not any(node == 'LabyClientTitleStartOnce' for node, _ in port.calls)


def test_notice_must_be_stable_before_close():
    port = Port()
    port.after_pages = ['LabyClientAfterTitleHomeObserved', 'LabyClientNoticeObserved', 'LabyClientNoticeObserved']
    recover(port)
    assert port.calls.count(('LabyGameSessionTitleStabilityWait', False)) == 2
    assert port.calls.count(('LabyClientNoticeCloseOnce', True)) == 1

@pytest.mark.parametrize('field,value', [('pending', {'intent_id': 'new'}), ('started', True), ('battle_start', 'sent')])
def test_recovery_cannot_claim_preserved_battle_when_a_callback_changes_it(field, value):
    port = Port()
    original = port._verify_account
    def changed(plan):
        original(plan)
        setattr(port, field, value)
    port._verify_account = changed
    with pytest.raises(RuntimeError, match='battle_state_changed'):
        recover(port)
    assert port._account_binding is None


def test_pending_object_and_its_content_remain_untouched_without_navigation():
    pending = {'intent_id': 'reserved', 'action': {'boss_instance': 'old'}}
    port = Port(start='unknown', pending=pending)
    before = deepcopy(pending)
    result = recover(port)
    assert result['can_continue'] is False and port.pending is pending and pending == before
    assert not port.calls


@pytest.mark.parametrize('mutation', ['action_missing', 'action_failed', 'action_input', 'child_missing',
    'child_wrong', 'label_wrong', 'low_score', 'nan_score', 'bad_box', 'outside_roi'])
def test_6002_requires_fixed_read_only_action_and_all_five_real_ocr_matches(mutation):
    port = Port()
    node = port.last_receipt['nodes'][0]
    row = node['recognition'][1]
    word = row['detail']['filtered'][0]
    if mutation == 'action_missing': node['action'] = None
    elif mutation == 'action_failed': node['action']['success'] = False
    elif mutation == 'action_input': node['action']['raw'] = {'click': [1, 2]}
    elif mutation == 'child_missing': node['recognition'].pop()
    elif mutation == 'child_wrong': row['algorithm'] = 'DirectHit'
    elif mutation == 'label_wrong': word['text'] = '错误代码：6001'
    elif mutation == 'low_score': word['score'] = .7
    elif mutation == 'nan_score': word['score'] = float('nan')
    elif mutation == 'bad_box': row['box'] = [0, 0, 0, 0]
    else: row['box'] = word['box'] = [0, 0, 2, 2]
    with pytest.raises(RuntimeError, match='disconnect_unproven'):
        recover(port)
    assert not port.calls


@pytest.mark.parametrize('start', ['not_sent', 'sent', 'unknown'])
def test_successful_input_in_triggering_receipt_never_gets_navigation_recovery(start):
    port = Port(start=start)
    port.last_receipt['nodes'].insert(0, receipt('CBStart', click=True)['nodes'][0])
    port.tasker.resource.get_node_data = lambda name: dict(action=dict(
        type='Click' if name == 'CBStart' else 'DoNothing', param={}))
    with pytest.raises(RuntimeError, match='disconnect_unproven'):
        recover(port)
    assert not port.calls


def test_failed_input_node_without_action_is_not_claimed_as_executed():
    port = Port()
    port.last_receipt['nodes'].insert(0, dict(name='CBAction_open_clan', completed=False, action=None))
    assert recover(port)['can_continue'] is True


def test_missing_current_node_definition_cannot_prove_read_only():
    port = Port()
    port.tasker.resource.get_node_data = lambda name: None
    with pytest.raises(RuntimeError, match='disconnect_unproven'):
        recover(port)
    assert not port.calls


def test_completed_node_with_missing_action_evidence_cannot_prove_no_input():
    port = Port()
    port.last_receipt['nodes'].insert(0, dict(name='CBAction_open_clan', completed=True, action=None))
    with pytest.raises(RuntimeError, match='disconnect_unproven'):
        recover(port)
    assert not port.calls
