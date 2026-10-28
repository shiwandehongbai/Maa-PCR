"""One bounded 6002 recovery on the existing Owner; never replay a battle input."""
from copy import deepcopy
import math
import re
import time

from . import native
from .labyrinth.game_session import _box, _root
from .labyrinth.session import reconnect_pipeline, title_player_id

DISCONNECT = 'LabyDisconnect6002Observed'
RECOVERY_PAGES = ('LabyDateChangedObserved', 'LabyClientTitleObserved', 'LabyClientGameHomeObserved')
AFTER_TITLE_PAGES = ('LabyDateChangedObserved', DISCONNECT, 'LabyClientNoticeObserved',
                     'LabyClientAfterTitleHomeObserved', 'LabyClientDownloadErrorObserved')


class ClanDisconnected(RuntimeError):
    """A successful read-only 6002 observation, not a failed native dispatch."""
    def __init__(self, receipt, battle_start):
        super().__init__('clan_battle_disconnected_6002:battle_start=' + battle_start)
        self.receipt, self.battle_start = receipt, battle_start


def observe_disconnect_after_failure(port, cause, *, input_attempted=False):
    """Probe a failed dispatch once; never replay it or unblock the Owner."""
    if not isinstance(cause, Exception) or type(input_attempted) is not bool:
        raise ValueError('clan_battle_recovery_probe_invalid')
    if getattr(port, '_clan_disconnect_probe_active', False):
        return None
    port._clan_disconnect_probe_active = True
    try:
        port.checkpoint()
        port.choose([], timeout=1000)
    except ClanDisconnected as error:
        error.original_error = cause
        error.replay_forbidden = input_attempted and not isinstance(cause, native.NativeInputNotSent)
        return error
    except Exception:
        # The original failure remains authoritative if the read-only probe also fails.
        return None
    finally:
        port._clan_disconnect_probe_active = False
    return None


def _disconnect_proven(node):
    # These are the installed fixed read-only rules; an action name is not its type.
    rules = reconnect_pipeline()
    rule = rules[DISCONNECT]
    action, rows = node.get('action'), node.get('recognition')
    if (rule.get('action') != 'DoNothing' or rule.get('next') != []
            or type(action) is not dict or action.get('name') != DISCONNECT
            or action.get('success') is not True or action.get('raw') != {}
            or type(rows) is not list or len(rows) != len(rule['all_of'])):
        return False
    for row, name in zip(rows, rule['all_of']):
        if type(row) is not dict or row.get('name') != name or row.get('algorithm') != 'OCR':
            return False
        detail = row.get('detail')
        words = detail.get('filtered') if type(detail) is dict else None
        if type(words) is not list or len(words) != 1 or type(words[0]) is not dict:
            return False
        word, label = words[0], rules[name]
        score, box, text = word.get('score'), word.get('box'), word.get('text')
        if (label.get('action') != 'DoNothing' or type(text) is not str
                or re.fullmatch(label['expected'], text) is None
                or type(score) not in (int, float) or not math.isfinite(score)
                or not label['threshold'] <= score <= 1 or not _box(box) or row.get('box') != box):
            return False
        x, y, width, height = label['roi']
        if not (x <= box[0] and y <= box[1] and box[0] + box[2] <= x + width
                and box[1] + box[3] <= y + height):
            return False
    return True


def _readonly_receipt(port, nodes):
    # An earlier sent Start may be retained, but this dispatch must have no input.
    get_node = getattr(getattr(port.tasker, 'resource', None), 'get_node_data', None)
    if not callable(get_node):
        return False
    for node in nodes:
        if type(node) is not dict:
            return False
        action = node.get('action')
        if action is None:
            if node.get('completed') is not False:
                return False
            continue
        try:
            configured = get_node(node.get('name'))
        except Exception:
            return False
        if (type(configured) is not dict or configured.get('action') != {'type': 'DoNothing', 'param': {}}
                or type(action) is not dict or action.get('name') != node.get('name')
                or node.get('completed') is not True or action.get('success') is not True
                or action.get('raw') != {}):
            return False
    return True


def _choice(receipt, entry, choices):
    rows = _root(receipt, entry).get('recognition')
    if (type(rows) is not list or not 1 <= len(rows) <= len(choices)
            or any(type(row) is not dict or row.get('algorithm') != 'And' for row in rows)
            or [row.get('name') for row in rows] != list(choices[:len(rows)])
            or any(row.get('box') is not None for row in rows[:-1])
            or not _box(rows[-1].get('box'))):
        raise RuntimeError('clan_battle_recovery_page_unknown')
    hit = rows[-1]['name']
    if hit == 'LabyDateChangedObserved':
        raise RuntimeError('clan_battle_recovery_game_day_changed')
    return hit


def _invalidate(port):
    revoke = getattr(port, 'invalidate_consumption_authorizations', None)
    if callable(revoke):
        revoke()
    port.invalidate_account_binding()
    page = getattr(port, 'page', None)
    reset = getattr(page, 'reset_context', None)
    if callable(reset):
        reset()
    elif page is not None:
        page.current, page.expected, page.bound = None, None, None
    port.opening_paused = False
    for name, empty in (('last_bosses', {}), ('stocks', []), ('boss_history', {})):
        if hasattr(port, name):
            setattr(port, name, empty)
    if hasattr(port, 'inheritance_recovered'):
        port.inheritance_recovered = False


def recover_disconnect(port, plan, error, *, pending=False):
    """Restore an observed session once; the caller restarts only safe observation.

    A sent/unknown Start or any pending intent always returns can_continue=False.
    No controller, source certificate, battle-start fact or Journal is replaced.
    """
    port.checkpoint()
    receipt = getattr(error, 'receipt', None)
    nodes = receipt.get('nodes') if type(receipt) is dict else None
    matches = [row for row in nodes if type(row) is dict and row.get('name') == DISCONNECT] if type(nodes) is list else []
    if (type(error) is not ClanDisconnected or type(receipt) is not dict
            or receipt is not getattr(port, 'last_receipt', None)
            or receipt.get('native_ok') is not True or receipt.get('tasker_inited') is not True
            or type(receipt.get('task_id')) is not int or receipt['task_id'] <= 0
            or len(matches) != 1 or matches[0].get('completed') is not True
            or not _disconnect_proven(matches[0]) or not _readonly_receipt(port, nodes)
            or error.battle_start != port.battle_start or type(pending) is not bool):
        raise RuntimeError('clan_battle_recovery_disconnect_unproven')
    expected = plan.get('account_id')
    if (type(expected) is not str or not expected.isascii() or not expected.isdecimal()
            or not 0 < int(expected) <= 2**63 - 1):
        raise RuntimeError('clan_battle_recovery_account_unknown')
    source = port._account_source()
    if source is None:
        raise RuntimeError('clan_battle_recovery_owner_required')
    if getattr(port, '_clan_disconnect_recovery_used', False):
        raise RuntimeError('clan_battle_recovery_already_attempted')
    port._clan_disconnect_recovery_used = True
    start, attempted = port.battle_start, getattr(port, 'started', False)
    pending_intent = getattr(port, 'pending', None)
    pending_snapshot = deepcopy(pending_intent)
    has_pending = pending or pending_intent is not None
    can_continue = (start == 'not_sent' and attempted is False and not has_pending
                    and not getattr(error, 'replay_forbidden', False))
    _invalidate(port)
    evidence = [deepcopy(receipt)]
    if start != 'not_sent' or attempted is not False or has_pending:
        # A possible battle remains untouched until its original result is known.
        result = dict(can_continue=False, account_id=expected, battle_start=start,
            pending_preserved=has_pending, account_evidence=None, navigation=evidence,
            original_result_unknown=True)
        port.last_disconnect_recovery = result
        return result
    expires = min(native.RUN_DEADLINE.get(), time.monotonic() + 120)
    token = native.RUN_DEADLINE.set(expires)

    def check():
        port.checkpoint()
        if time.monotonic() >= expires:
            raise RuntimeError('clan_battle_recovery_timeout')
        if port._account_source() != source:
            raise RuntimeError('clan_battle_recovery_owner_changed')

    def run(node, *, click=False):
        check()
        result = port.run(node, click=click)
        check()
        _root(result, node)
        if click:
            native.require_native_input(result, (node,))
        evidence.append(deepcopy(result))
        return result

    try:
        run('LabyDisconnectReturnTitleOnce', click=True)
        hit = _choice(run('LabyGameSessionRecoveryObserved'), 'LabyGameSessionRecoveryObserved', RECOVERY_PAGES)
        if hit == 'LabyClientTitleObserved':
            title = run('LabyClientTitleObserved')
            if str(title_player_id(title)) != expected:
                raise RuntimeError('clan_battle_account_mismatch')
            run('LabyClientTitleStartOnce', click=True)
            previous = None
            startup_expires = min(expires, time.monotonic() + 75)
            while True:
                check()
                remaining = startup_expires - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('clan_battle_recovery_login_unverified')
                entry = 'LabyGameSessionAfterTitleObserved'
                if not port.tasker.resource.override_pipeline({entry: dict(timeout=max(1, int(remaining * 1000)))}):
                    raise RuntimeError('clan_battle_recovery_pipeline_rejected')
                hit = _choice(run(entry), entry, AFTER_TITLE_PAGES)
                if hit == DISCONNECT:
                    raise RuntimeError('clan_battle_recovery_disconnected_again')
                if hit == 'LabyClientDownloadErrorObserved':
                    raise RuntimeError('clan_battle_recovery_login_unverified')
                if hit == previous:
                    break
                previous = hit
                run('LabyGameSessionTitleStabilityWait')
            if hit == 'LabyClientNoticeObserved':
                run('LabyClientNoticeCloseOnce', click=True)
                run('LabyClientGameHomeObserved')
        run('LabyClientHomeToAdventure', click=True)
        run('LabyClientAdventureObserved')
        run('LabyClientOpenMainMenuOnce', click=True)
        run('LabyClientMainMenuObserved')
        check()
        port._verify_account(plan)
        check()
        if not port.account_binding_valid(plan):
            raise RuntimeError('clan_battle_recovery_account_unverified')
        if (port.battle_start != start or getattr(port, 'started', False) is not attempted
                or getattr(port, 'pending', None) is not pending_intent or pending_intent != pending_snapshot):
            raise RuntimeError('clan_battle_recovery_battle_state_changed')
        result = dict(can_continue=can_continue, account_id=expected, battle_start=start,
            pending_preserved=has_pending, account_evidence=deepcopy(port.account_receipt), navigation=evidence)
        port.last_disconnect_recovery = result
        return result
    except Exception:
        _invalidate(port)
        raise
    finally:
        native.RUN_DEADLINE.reset(token)
