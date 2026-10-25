"""Owner-only clan simulation port. Uncalibrated clan pages never provide input."""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time

from maa.custom_recognition import CustomRecognition
from copy import deepcopy

from . import native
from .guide_trial_native import NativeGuideTrial
from .labyrinth.game_session import game_session_pipeline
from .labyrinth.profile import PROFILE_NODE, profile_player_id


PAGES = ('adventure', 'lobby', 'target', 'formation', 'support', 'battle', 'result', 'closed')
V2_PAGES = (*PAGES, 'boss_detail', 'difficulty')
FIELDS = {
    'period': r'^20[0-9]{2}[-年](?:0[1-9]|1[0-2])月?$',
    'phase': r'^(?:B|C|BC|D|E)$',
    'round': r'^(?:第\s*)?[1-9][0-9]*(?:\s*(?:周目|轮))?$',
    'boss': r'^(?:BOSS\s*)?[1-5]$',
    'simulation': r'^模拟战$',
    'damage': r'^[0-9][0-9,，\s]*$',
    'remaining_seconds': r'^(?:1[:：](?:[0-2][0-9]|30)|0[:：][0-5][0-9])$',
}
ACTION_PAGES = dict(open_clan='adventure', open_boss='lobby', open_simulation='target',
                    open_support='formation', support_back='support', start='formation',
                    characters_top='formation', characters_next='formation',
                    support_top='support', support_next='support', return_to_target='result')
V2_ACTION_PAGES = dict(ACTION_PAGES, open_simulation='boss_detail', open_formation='target',
                      back_to_lobby='boss_detail', back_to_adventure='lobby',
                      open_difficulty='boss_detail', select_difficulty='difficulty',
                      confirm_difficulty='difficulty', disable_live_state='target')
V2_DESTINATIONS = dict(open_clan='lobby', open_boss='boss_detail', open_simulation='target',
                       open_formation='formation', open_support='support', support_back='formation',
                       start='battle', return_to_target='lobby', open_difficulty='difficulty',
                       select_difficulty='difficulty', confirm_difficulty='boss_detail', disable_live_state='target')
IDENTITY_FIELDS = ('period', 'phase', 'round', 'boss', 'simulation')
CALIBRATION_GAPS = (
    '公会战入口与关闭页面', '期次、阶段、周目和BOSS编号', '模拟战模式与正式战排除标记',
    '编队空位、星级、自有/支援标记及列表翻页', '支援编号、星级及可用状态',
    '模拟结果伤害页及返回模拟目标后继', '会战HUD与秒级读取/输入延迟',
)


def _roi(value):
    if (type(value) is not list or len(value) != 4 or any(type(n) is not int for n in value)
            or value[0] < 0 or value[1] < 0 or value[2] <= 0 or value[3] <= 0
            or value[0]+value[2] > 1280 or value[1]+value[3] > 720):
        raise ValueError('clan_battle_calibration_roi_invalid')
    return value


def _node(spec, *, expected=None):
    if type(spec) is not dict:
        raise ValueError('clan_battle_calibration_node_invalid')
    algorithm = spec.get('recognition', 'OCR')
    value = dict(recognition=algorithm, roi=_roi(spec.get('roi')), action='DoNothing', next=[],
                 timeout=1000, pre_delay=0, post_delay=0)
    if algorithm == 'OCR':
        text = expected if expected is not None else spec.get('expected')
        if type(text) is not str or not text.startswith('^') or not text.endswith('$'):
            raise ValueError('clan_battle_calibration_label_invalid')
        re.compile(text)
        value.update(expected=text, threshold=.9)
    elif algorithm == 'TemplateMatch' and expected is None:
        name, digest = spec.get('template'), spec.get('sha256')
        names, digests = (name, digest) if type(name) is list else ([name], [digest])
        if type(names) is not list or not 1 <= len(names) <= 16 or type(digests) is not list or len(names) != len(digests):
            raise ValueError('clan_battle_calibration_template_invalid')
        for filename, expected_digest in zip(names, digests):
            if type(filename) is not str or Path(filename).is_absolute() or '..' in Path(filename).parts:
                raise ValueError('clan_battle_calibration_template_invalid')
            path = native.RESOURCE / 'image' / filename
            if (type(expected_digest) is not str or not re.fullmatch('[0-9a-f]{64}', expected_digest)
                    or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest):
                raise ValueError('clan_battle_calibration_template_changed')
        value.update(template=name, threshold=.9)
    else:
        raise ValueError('clan_battle_calibration_algorithm_invalid')
    return value


def _difficulty_choice(calibration, plan):
    choices = calibration['actions']['select_difficulty'].get('choices')
    if (type(choices) is not dict or not choices or not set(choices) <= {'B', 'C', 'D', 'E'}
            or any(type(row) is not dict or set(row) != {'label', 'selected'} for row in choices.values())):
        raise ValueError('clan_battle_calibration_difficulty_invalid')
    allowed = ('B', 'C') if plan['phase'] == 'BC' else (plan['phase'],)
    phase = next((phase for phase in allowed if phase in choices), None)
    if phase is None:
        raise RuntimeError('clan_battle_difficulty_unavailable')
    return choices[phase]


def auto_battle_pipeline(calibration, plan):
    spec = calibration.get('auto_battle')
    if spec is None:
        return {}
    if (calibration.get('version') != 2 or type(spec) is not dict
            or set(spec) != {'speeds', 'opening_guards', 'paused_guards', 'auto_on', 'auto_off', 'speed', 'set_badge', 'empty_tp', 'samples'}
            or type(spec['speeds']) is not list or not spec['speeds'] or 1 not in spec['speeds']
            or any(type(n) is not int or n not in (1, 2, 4) for n in spec['speeds'])
            or len(set(spec['speeds'])) != len(spec['speeds'])
            or type(spec['speed']) is not dict or set(spec['speed']) != {'1', '2', '4'}):
        raise ValueError('clan_battle_auto_calibration_invalid')
    if any(key not in calibration['pages']['battle'] for speed, key in ((2, 'speed_two'), (4, 'speed_four')) if speed in spec['speeds']):
        raise ValueError('clan_battle_auto_speed_calibration_missing')
    nodes = {}
    for group, count in (('opening_guards', 3), ('paused_guards', 4)):
        if type(spec[group]) is not list or len(spec[group]) != count:
            raise ValueError('clan_battle_auto_calibration_invalid')
        for index, marker in enumerate(spec[group]):
            nodes[f'CBOpening_{group}_{index}'] = _node(marker)
    clock = spec['paused_guards'][2]
    if (clock.get('recognition') != 'TemplateMatch' or type(clock.get('template')) is not list
            or len(clock['template']) != 2 or type(clock.get('sha256')) is not list or len(clock['sha256']) != 2):
        raise ValueError('clan_battle_auto_clock_calibration_invalid')
    for index, second in enumerate((90, 89)):
        marker = dict(clock, template=clock['template'][index], sha256=clock['sha256'][index])
        nodes['CBOpeningClock'+str(second)] = _node(marker)
    nodes['CBOpeningBoss'] = _node(dict(roi=[805, 160, 135, 33]),
        expected='^' + re.escape(calibration['boss_targets'][str(plan['boss'])]['name']) + '$')
    for name in ('auto_on', 'auto_off'):
        nodes['CBOpening_'+name] = _node(spec[name])
    for speed, marker in spec['speed'].items():
        nodes['CBOpeningSpeed'+speed] = _node(marker, expected='^'+speed+'倍速$')
    for index in range(5):
        marker = deepcopy(spec['set_badge'])
        marker['roi'] = _roi(marker['roi']).copy()
        marker['roi'][0] += 116*index
        nodes['CBOpeningSet'+str(index)] = _node(marker)
        marker = deepcopy(spec['empty_tp'])
        marker['roi'] = _roi(marker['roi']).copy()
        marker['roi'][0] += 116*index
        nodes['CBOpeningEmptyTp'+str(index)] = _node(marker)
    for name, mode, click in (('CBOpeningRead', 'opening_read', False), ('CBOpeningConfirmed', 'opening_read', False),
            ('CBOpeningAuto', 'opening_auto', True), ('CBOpeningSet', 'opening_set', True),
            ('CBOpeningSpeed', 'opening_speed', True), ('CBResumeOpening', 'opening_resume', True)):
        nodes[name] = dict(recognition='Custom', custom_recognition='ClanBattleCharacters',
            custom_recognition_param=dict(mode=mode), action='Click' if click else 'DoNothing', target=True,
            next=['CBOpeningConfirmed'] if name in ('CBOpeningAuto', 'CBOpeningSet', 'CBOpeningSpeed') else [],
            timeout=15000, pre_delay=0, post_delay=300 if click else 0,
            on_error=['LabyDisconnect6002Observed'])
        if click:
            nodes[name]['max_hit'] = 1
    # The fading intro already shows 90s but ignores Menu; only the bright button accepts input.
    nodes['CBOpeningInputReady'] = dict(recognition='ColorMatch', roi=[1180, 22, 45, 24], method=4,
        lower=[220, 220, 220], upper=[255, 255, 255], count=400, action='DoNothing', next=[], pre_delay=0, post_delay=0)
    # Native same-frame guards avoid three image IPC round trips in the short 4x opening.
    nodes['CBPauseOpening'] = dict(recognition='And',
        all_of=['CBOpeningInputReady', *[f'CBOpening_opening_guards_{i}' for i in range(3)]], action='Click', target=[1204, 31, 6, 6],
        max_hit=1, next=['CBOpeningRead'], timeout=15000, rate_limit=1, pre_delay=0, post_delay=300,
        on_error=['LabyDisconnect6002Observed'])
    return nodes


def require_battle_speed(calibration, plan, battle_speed):
    if type(battle_speed) is not int or battle_speed not in (1, 2, 4):
        raise ValueError('clan_battle_speed_invalid')
    if battle_speed != 1 and (plan.get('switches') or battle_speed not in calibration.get('auto_battle', {}).get('speeds', [])):
        raise RuntimeError('clan_battle_auto_speed_uncalibrated')


def calibration_pipeline(calibration, plan):
    """Explicit injection for saved-frame checks; no discovered file enables it."""
    if calibration is None:
        raise RuntimeError('clan_battle_calibration_missing:' + '、'.join(CALIBRATION_GAPS))
    if (type(calibration) is not dict or type(calibration.get('version')) is not int
            or calibration['version'] not in (1, 2)
            or calibration.get('frame_size') != [1280, 720]
            or calibration.get('battle_hud') != 'guide-trial-1280x720'
            or calibration.get('server') != plan['server']
            or not calibration.get('calibration_id')
            or calibration.get('evidence_kind') not in ('offline_replay', 'device_saved_frame')):
        raise ValueError('clan_battle_calibration_invalid')
    modern = calibration['version'] == 2
    support_scope = calibration.get('support_modes')
    if 'support_modes' in calibration and (not modern or support_scope != ['none']):
        raise ValueError('clan_battle_calibration_support_scope_invalid')
    own_only = support_scope == ['none']
    if own_only and plan.get('support_mode') != 'none':
        raise RuntimeError('clan_battle_support_mode_uncalibrated')
    page_names = V2_PAGES if modern else PAGES
    action_pages = V2_ACTION_PAGES if modern else ACTION_PAGES
    if own_only:
        action_pages = {name: page for name, page in action_pages.items()
                        if name not in ('open_support', 'support_back', 'support_top', 'support_next')}
    if modern:
        targets, phases = calibration.get('boss_targets'), calibration.get('phase_labels')
        if (type(targets) is not dict or set(targets) != set('12345')
                or any(type(row) is not dict or set(row) != {'target', 'name'}
                    or type(row['name']) is not str or not 0 < len(row['name']) <= 100 for row in targets.values())):
            raise ValueError('clan_battle_calibration_boss_targets_invalid')
        for row in targets.values():
            _roi(row['target'])
        if len({tuple(row['target']) for row in targets.values()}) != 5 or len({row['name'] for row in targets.values()}) != 5:
            raise ValueError('clan_battle_calibration_boss_targets_invalid')
        if (type(phases) is not dict or not phases or any(type(key) is not str or not key
                or value not in ('B', 'C', 'D', 'E') for key, value in phases.items())):
            raise ValueError('clan_battle_calibration_phase_labels_invalid')
    samples = calibration.get('samples')
    if type(samples) is not list or not samples:
        raise ValueError('clan_battle_calibration_samples_missing')
    covered = set()
    for sample in samples:
        if type(sample) is not dict or sample.get('page') not in page_names:
            raise ValueError('clan_battle_calibration_sample_invalid')
        path = Path(sample.get('path', ''))
        if (not path.is_absolute() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != sample.get('sha256')):
            raise ValueError('clan_battle_calibration_sample_changed')
        native.read_image(path)
        covered.add(sample['page'])
    required_samples = set(page_names) - ({'closed'} if modern else set())
    if not required_samples <= covered:
        raise ValueError('clan_battle_calibration_pages_missing')
    pages = calibration.get('pages')
    if (type(pages) is not dict or not set(page_names) - ({'closed'} if modern else set()) <= set(pages)
            or not set(pages) <= set(page_names)):
        raise ValueError('clan_battle_calibration_pages_missing')
    nodes = {}
    if modern and 'closed' not in pages:
        # An unavailable closed-event sample must not be replaced by invented positive guards.
        nodes['CBPage_closed'] = dict(recognition='Custom', custom_recognition='ClanBattlePage',
            custom_recognition_param=dict(page='closed'), action='DoNothing', next=[], timeout=1000,
            pre_delay=0, post_delay=0)
    for page, spec in pages.items():
        if type(spec) is not dict or type(spec.get('labels')) is not list or len(spec['labels']) < 2:
            raise ValueError('clan_battle_calibration_page_guards_missing')
        for index, label in enumerate(spec['labels']):
            nodes[f'CBLabel_{page}_{index}'] = _node(label)
        fields = spec.get('fields', {})
        if type(fields) is not dict:
            raise ValueError('clan_battle_calibration_fields_missing:' + page)
        required = set(IDENTITY_FIELDS if page in ('target', 'formation', 'support', 'result')
                       else ('period', 'phase', 'round') if page == 'lobby'
                       else ('remaining_seconds',) if page == 'battle' else ())
        inherited = spec.get('inherit', [])
        if modern:
            required = set(('period', 'phase', 'boss', 'simulation') if page in ('target', 'formation', 'support', 'result')
                           else ('period', 'phase', 'boss') if page == 'boss_detail'
                           else ('period', 'boss') if page == 'difficulty'
                           else ('period',) if page == 'lobby' else ('remaining_seconds',) if page == 'battle' else ())
            if (type(inherited) is not list or any(type(key) is not str for key in inherited)
                    or len(set(inherited)) != len(inherited)
                    or any(key not in required for key in inherited) or set(inherited) & set(fields)):
                raise ValueError('clan_battle_calibration_inheritance_invalid:' + page)
            visible = {'period'} if page == 'lobby' else {'phase', 'boss'} if page in ('boss_detail', 'target') else set()
            if page in ('target', 'formation', 'support', 'result'):
                visible.add('simulation')
            if not visible <= set(fields):
                raise ValueError('clan_battle_calibration_fields_missing:' + page)
        if page == 'result':
            required.add('damage')
        if type(fields) is not dict or set(fields) | set(inherited) != required:
            raise ValueError('clan_battle_calibration_fields_missing:' + page)
        for field, roi in fields.items():
            expected = FIELDS.get(field)
            if modern:
                if field == 'boss':
                    expected = '^' + re.escape(targets[str(plan['boss'])]['name']) + '$'
                elif field == 'phase':
                    expected = '^(?:' + '|'.join(re.escape(key) for key in phases)
                    expected += r'|第[1-5]阶段)$' if page == 'boss_detail' else ')$'
                elif field == 'simulation':
                    expected = (r'^模拟战的战斗结果不会显示在团队战成绩中[。.]?$' if page == 'result'
                                else r'^设定为模拟战[。.]?$')
                elif field == 'period':
                    expected = r'^(?:20[0-9]{2}[-年](?:0[1-9]|1[0-2])月?|[0-1]?[0-9]/[0-3]?[0-9]\s*[0-2][0-9]:[0-5][0-9]\s*[~～〜至—－-]\s*[0-1]?[0-9]/[0-3]?[0-9]\s*[0-2][0-9]:[0-5][0-9])$'
            nodes[f'CBField_{page}_{field}'] = _node(dict(roi=roi), expected=expected)
        if modern and page == 'battle':
            nodes['CBOneSpeed'] = _node(spec.get('speed_one'))
            for speed, key in ((2, 'speed_two'), (4, 'speed_four')):
                if key in spec:
                    nodes['CBBattleSpeed'+str(speed)] = _node(spec[key])
        if modern and page == 'target':
            states = spec.get('live_state')
            if type(states) is not dict or set(states) != {'on', 'off', 'absent'}:
                raise ValueError('clan_battle_calibration_simulation_settings_missing')
            for state, marker in states.items():
                nodes['CBLiveState_'+state] = _node(marker)
        if page in ('formation', 'support'):
            nodes[f'CBFormal_{page}'] = _node(spec.get('formal'), expected=r'^(?:实战|设定为实战[。.]?|正式战|正式挑战|消耗挑战次数)$')
        nodes['CBPage_'+page] = dict(recognition='Custom', custom_recognition='ClanBattlePage',
            custom_recognition_param=dict(page=page), action='DoNothing', next=[], timeout=1000,
            pre_delay=0, post_delay=0)
        if modern and page in ('lobby', 'boss_detail'):
            nodes['CBObserve_'+page] = dict(nodes['CBPage_'+page],
                custom_recognition_param=dict(page=page, observe_only=True))
    actions = calibration.get('actions')
    if type(actions) is not dict or set(actions) != set(action_pages):
        raise ValueError('clan_battle_calibration_actions_missing')
    if modern:
        selected = _difficulty_choice(calibration, plan)
        nodes['CBDifficultySelected'] = _node(selected['selected'])
    for name, page in action_pages.items():
        spec = actions[name]
        if type(spec) is not dict or spec.get('page') != page:
            raise ValueError('clan_battle_calibration_action_page_invalid')
        if name in ('characters_top', 'characters_next', 'support_top', 'support_next'):
            begin, end = spec.get('begin'), spec.get('end')
            _roi(begin); _roi(end)
            action = dict(action='Swipe', begin=begin, end=end, duration=400)
        elif modern and name in ('open_boss', 'back_to_lobby', 'back_to_adventure', 'disable_live_state'):
            _roi(targets[str(plan['boss'])]['target'] if name == 'open_boss' else spec.get('target'))
            action = dict(action='Click', target=True)
        else:
            label = dict(selected['label'] if modern and name == 'select_difficulty' else spec.get('label', {}))
            if name == 'open_simulation':
                label['expected'] = '^模拟战$'
            elif name == 'open_boss':
                label['expected'] = rf'^BOSS\s*{plan["boss"]}$'
            elif name == 'start':
                label['expected'] = '^(?:模拟战开始|开始模拟战|战斗开始)$'
            elif name == 'open_formation':
                label['expected'] = '^挑战$'
            elif name == 'open_difficulty':
                label['expected'] = '^难度变更$'
            elif name == 'confirm_difficulty':
                label['expected'] = '^确认$'
            nodes['CBActionLabel_'+name] = _node(label)
            action = dict(action='Click', target=True)
        if name == 'start':
            # Only CBStart is actionable; its character callback rechecks all five slots.
            continue
        params = dict(page=page, action=name)
        if modern and name in ('back_to_lobby', 'back_to_adventure'):
            params['observe_only'] = True
        nodes['CBAction_'+name] = dict(recognition='Custom', custom_recognition='ClanBattlePage',
            custom_recognition_param=params, next=[], timeout=1000,
            max_hit=1, pre_delay=0, post_delay=200, **action)
    if modern:
        nodes['CBDifficultyChosen'] = dict(nodes['CBPage_difficulty'],
            custom_recognition_param=dict(page='difficulty', require_selected_difficulty=True))
        nodes['CBFullSimulationReady'] = dict(nodes['CBPage_target'],
            custom_recognition_param=dict(page='target', require_full_simulation=True))
    for section in ('formation', 'support'):
        if own_only and section == 'support':
            continue
        layout = calibration.get(section)
        if type(layout) is not dict or type(layout.get('slots')) is not list or not layout['slots']:
            raise ValueError('clan_battle_calibration_slots_missing')
        for roi in layout['slots']:
            _roi(roi)
        if section == 'formation':
            if ('star_source' in layout and (not modern or not own_only
                    or layout['star_source'] != 'account_snapshot')):
                raise ValueError('clan_battle_calibration_star_source_invalid')
            markers = ('empty', 'stars', 'borrowed') if modern else ('empty', 'stars', 'owned', 'borrowed')
            if (len(layout['slots']) != 5 or any(type(layout.get(key)) is not list or len(layout[key]) != 5
                    for key in markers)
                    or type(layout.get('candidates')) is not list or not layout['candidates']):
                raise ValueError('clan_battle_calibration_formation_invalid')
            for roi in layout['candidates']:
                _roi(roi)
            for index in range(5):
                for key, prefix in (('empty', 'CBEmpty'), ('owned', 'CBOwned'), ('borrowed', 'CBSupport')):
                    if modern and key == 'owned':
                        continue
                    nodes[prefix+str(index)] = _node(layout[key][index])
                for star in range(1, 7):
                    nodes[f'CBStar{index}_{star}'] = _node(dict(roi=layout['stars'][index]), expected=rf'^{star}$')
        else:
            for key in ('stars', 'ids', 'available', 'unavailable'):
                if type(layout.get(key)) is not list or len(layout[key]) != len(layout['slots']):
                    raise ValueError('clan_battle_calibration_support_invalid')
            for index in range(len(layout['slots'])):
                nodes[f'CBSupportId{index}'] = _node(dict(roi=layout['ids'][index]), expected=r'^[0-9]{1,19}$')
                nodes[f'CBSupportAvailable{index}'] = _node(layout['available'][index])
                nodes[f'CBSupportUnavailable{index}'] = _node(layout['unavailable'][index])
                for star in range(1, 7):
                    nodes[f'CBSupportStar{index}_{star}'] = _node(dict(roi=layout['stars'][index]), expected=rf'^{star}$')
        for position in ('top', 'bottom'):
            nodes[f'CBList_{section}_{position}'] = _node(layout.get(position))
    for name, mode, click in (('CBReadTeam', 'read', False), ('CBEditTeam', 'read', True),
                              ('CBReadSupport', 'support_read', False), ('CBAddSupport', 'support_add', True),
                              ('CBStart', 'start', True), ('CBReadSettings', 'battle_read', False),
                              ('CBSettingsConfirmed', 'battle_read', False),
                              ('CBToggleAuto', 'battle_auto', True), ('CBToggleSet', 'battle_set', True)):
        if own_only and name in ('CBReadSupport', 'CBAddSupport'):
            continue
        nodes[name] = dict(recognition='Custom', custom_recognition='ClanBattleCharacters',
            custom_recognition_param=dict(mode=mode), action='Click' if click else 'DoNothing', target=True,
            next=['CBSettingsConfirmed'] if name in ('CBToggleAuto', 'CBToggleSet') else [],
            timeout=1000, pre_delay=0, post_delay=0)
        if click:
            nodes[name]['max_hit'] = 1
    nodes['CBSettingsConfirmed']['on_error'] = ['LabyDisconnect6002Observed']
    nodes.update(auto_battle_pipeline(calibration, plan))
    if calibration.get('auto_battle') and not plan['switches']:
        nodes['CBStart'].update(next=['CBPauseOpening'], timeout=30000, rate_limit=1)
    return nodes


def require_target_identity(plan, reading):
    phase = reading.get('phase')
    difficulty = reading.get('selected_difficulty')
    virtual_round = (reading.get('round') is None and reading.get('round_source') == 'selected_difficulty'
                     and difficulty == dict(phase=phase, boss=reading.get('boss'), simulation=True))
    observed_round = (type(reading.get('round')) is int and reading['round'] >= plan['round_min']
                      and (plan.get('round_max') is None or reading['round'] <= plan['round_max']))
    if (reading.get('period') != plan['period']
            or phase not in (('B', 'C', 'BC') if plan['phase'] == 'BC' else (plan['phase'],))
            or not (virtual_round or observed_round)
            or type(reading.get('boss')) is not int or reading['boss'] != plan['boss']
            or reading.get('simulation') is not True):
        raise RuntimeError('clan_battle_target_mismatch_or_not_simulation')


def _current_period(text):
    if '/' not in text:
        return text.replace('年', '-').replace('月', '')
    match = re.fullmatch(r'([0-1]?[0-9])/([0-3]?[0-9])\s*([0-2][0-9]):([0-5][0-9])\s*[~～〜至—－-]\s*'
                         r'([0-1]?[0-9])/([0-3]?[0-9])\s*([0-2][0-9]):([0-5][0-9])', text)
    if match is None:
        raise RuntimeError('clan_battle_period_unknown')
    now = datetime.now(timezone(timedelta(hours=8)))
    month, day, hour, minute, end_month, end_day, end_hour, end_minute = map(int, match.groups())
    try:
        start = now.replace(month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
        end = now.replace(month=end_month, day=end_day, hour=end_hour, minute=end_minute, second=59, microsecond=999999)
    except ValueError as exc:
        raise RuntimeError('clan_battle_period_unknown') from exc
    if not start <= now <= end:
        raise RuntimeError('clan_battle_period_not_current')
    return start.strftime('%Y-%m')


class ClanBattlePage(CustomRecognition):
    def __init__(self, plan, calibration, support_unit_id=None):
        super().__init__()
        self.plan, self.calibration, self.support_unit_id = plan, calibration, support_unit_id
        self.modern = calibration.get('version') == 2
        self.reset_context()

    def reset_context(self):
        self.bound, self.current_page, self.expected_page = {}, None, None

    def advance(self, action):
        if self.modern:
            self.expected_page = V2_DESTINATIONS.get(action, self.current_page)

    def hit(self, context, image, name):
        seen = context.run_recognition(name, image)
        return seen is not None and seen.hit is True

    def word(self, context, image, name):
        seen = context.run_recognition(name, image)
        raw = getattr(seen, 'raw_detail', None) if seen is not None and seen.hit else None
        words = raw.get('filtered') if type(raw) is dict else None
        if (type(words) is not list or len(words) != 1 or type(words[0]) is not dict
                or raw.get('best') != words[0] or type(words[0].get('text')) is not str
                or type(words[0].get('score')) not in (int, float)
                or not math.isfinite(words[0]['score']) or not .9 <= words[0]['score'] <= 1):
            raise RuntimeError('clan_battle_page_field_unknown:' + name)
        node = context.get_node_data(name)
        if type(node) is not dict:
            raise RuntimeError('clan_battle_page_field_unknown:' + name)
        if type(node.get('recognition')) is dict:
            recognition = node['recognition']
            node = recognition.get('param') if recognition.get('type') == 'OCR' else None
            if type(node) is not dict:
                raise RuntimeError('clan_battle_page_field_unknown:' + name)
        expected = node.get('expected')
        if type(expected) is list:
            expected = expected[0] if len(expected) == 1 else None
        if type(expected) is not str:
            raise RuntimeError('clan_battle_page_field_unknown:' + name)
        box = words[0].get('box')
        roi = _roi(node.get('roi'))
        if (type(box) is not list or len(box) != 4 or any(type(n) is not int for n in box)
                or box[2] <= 0 or box[3] <= 0 or box[0] < roi[0] or box[1] < roi[1]
                or box[0]+box[2] > roi[0]+roi[2] or box[1]+box[3] > roi[1]+roi[3]
                or re.fullmatch(expected, words[0]['text']) is None):
            raise RuntimeError('clan_battle_page_field_unknown:' + name)
        return words[0]['text']

    def read(self, context, image, page, *, observe_only=False):
        if (context.tasker.stopping or image is None or image.shape != (720, 1280, 3)
                or page not in (V2_PAGES if self.modern else PAGES)):
            raise RuntimeError('clan_battle_page_unknown')
        if self.hit(context, image, 'LabyDisconnect6002Observed'):
            raise RuntimeError('clan_battle_disconnected_6002')
        spec = self.calibration['pages'].get(page)
        if spec is None:
            raise RuntimeError('clan_battle_page_uncalibrated:' + page)
        if not all(self.hit(context, image, f'CBLabel_{page}_{index}') for index in range(len(spec['labels']))):
            raise RuntimeError('clan_battle_page_unknown:' + page)
        if observe_only:
            if not self.modern or page not in ('lobby', 'boss_detail'):
                raise RuntimeError('clan_battle_observation_scope_invalid')
            return dict(page=page)
        # A boss detail offers both battle modes; only a selected formal mode is forbidden.
        if page in ('formation', 'support') and self.hit(context, image, 'CBFormal_'+page):
            raise RuntimeError('clan_battle_formal_battle_forbidden')
        values = {}
        if self.modern:
            observed_transition = (self.current_page, page) in (('boss_detail', 'target'), ('battle', 'result'))
            if page not in ('adventure', 'lobby', 'closed') and page not in (self.current_page, self.expected_page) and not observed_transition:
                raise RuntimeError('clan_battle_page_transition_unverified:' + page)
            for field in spec.get('inherit', []):
                if field not in self.bound:
                    raise RuntimeError('clan_battle_page_identity_missing:' + field)
                values[field] = self.bound[field]
        for field in spec.get('fields', {}):
            text = self.word(context, image, f'CBField_{page}_{field}')
            if field == 'simulation':
                value = (text.startswith('模拟战的战斗结果不会显示在团队战成绩中') if page == 'result'
                         else text.startswith('设定为模拟战')) if self.modern else text == '模拟战'
            elif field == 'period':
                value = _current_period(text) if self.modern else text.replace('年', '-').replace('月', '')
            elif field == 'phase':
                value = self.calibration['phase_labels'].get(text) if self.modern else text
            elif field == 'boss' and self.modern:
                if text != self.calibration['boss_targets'][str(self.plan['boss'])]['name']:
                    raise RuntimeError('clan_battle_boss_unknown')
                value = self.plan['boss']
            elif field == 'remaining_seconds':
                minute, second = text.replace('：', ':').split(':')
                value = int(minute)*60+int(second)
            else:
                value = int(re.sub('[^0-9]', '', text))
            values[field] = value
        if self.modern:
            if page == 'battle':
                speeds = [speed for speed, key in ((1, 'speed_one'), (2, 'speed_two'), (4, 'speed_four'))
                    if key in spec and self.hit(context, image, 'CBOneSpeed' if speed == 1 else 'CBBattleSpeed'+str(speed))]
                values['battle_speed'] = speeds[0] if len(speeds) == 1 else None
            if page == 'target':
                states = [state for state in ('on', 'off', 'absent') if self.hit(context, image, 'CBLiveState_'+state)]
                if len(states) != 1:
                    raise RuntimeError('clan_battle_simulation_settings_unknown')
                values['live_state'] = states[0]
                values.update(round=None, round_source='selected_difficulty', selected_difficulty=dict(
                    phase=values['phase'], boss=values['boss'], simulation=values['simulation']))
            elif page in ('formation', 'support', 'result'):
                for field in ('round', 'round_source', 'selected_difficulty'):
                    if field not in self.bound:
                        raise RuntimeError('clan_battle_page_identity_missing:' + field)
                    values[field] = deepcopy(self.bound[field])
            if page in ('lobby', 'boss_detail') and values['period'] != self.plan['period']:
                raise RuntimeError('clan_battle_target_mismatch_or_not_simulation')
        if page in ('target', 'formation', 'support', 'result'):
            require_target_identity(self.plan, values)
        if page == 'lobby' and not self.modern:
            require_target_identity(self.plan, dict(values, boss=self.plan['boss'], simulation=True))
        if self.modern:
            self.bound.update(deepcopy(values))
            self.current_page, self.expected_page = page, None
        return dict(values, page=page)

    def action(self, context, image, name, *, observe_only=False):
        if observe_only and name not in ('back_to_lobby', 'back_to_adventure'):
            raise RuntimeError('clan_battle_observation_scope_invalid')
        page = (V2_ACTION_PAGES if self.modern else ACTION_PAGES)[name]
        reading = self.read(context, image, page, observe_only=observe_only)
        if self.modern and name == 'open_formation' and reading.get('live_state') not in ('off', 'absent'):
            raise RuntimeError('clan_battle_simulation_settings_not_ready')
        if self.modern and name == 'disable_live_state' and reading.get('live_state') != 'on':
            raise RuntimeError('clan_battle_simulation_settings_changed')
        if self.modern and name == 'confirm_difficulty' and not self.hit(context, image, 'CBDifficultySelected'):
            raise RuntimeError('clan_battle_difficulty_selection_unverified')
        if self.modern and name == 'open_boss':
            return _roi(self.calibration['boss_targets'][str(self.plan['boss'])]['target'])
        if self.modern and name in ('back_to_lobby', 'back_to_adventure', 'disable_live_state'):
            return _roi(self.calibration['actions'][name]['target'])
        if name in ('characters_top', 'characters_next', 'support_top', 'support_next'):
            return [0, 0, 1, 1]
        node = 'CBActionLabel_'+name
        if not self.hit(context, image, node):
            raise RuntimeError('clan_battle_action_label_unknown:' + name)
        result = context.run_recognition(node, image)
        return _roi(list(result.box))

    def require_team(self, reading):
        ids, stars = reading.get('unit_ids'), reading.get('stars')
        if (type(ids) is not list or len(ids) != 5 or len(set(ids)) != 5 or set(ids) != set(self.plan['unit_ids'])
                or any(type(uid) is not int for uid in ids)
                or type(stars) is not list or len(stars) != 5
                or any(type(star) is not int for star in stars)
                or dict(zip(ids, stars)) != dict(zip(self.plan['unit_ids'], self.plan['stars']))
                or reading.get('member_count') != 5 or reading.get('support_unit_id') != self.support_unit_id):
            raise RuntimeError('clan_battle_team_or_support_mismatch')
        require_target_identity(self.plan, reading)

    def analyze(self, context, argv):
        try:
            params = json.loads(argv.custom_recognition_param)
            observed = params.get('observe_only', False)
            detail = self.read(context, argv.image, params.get('page'), observe_only=observed)
            if params.get('require_selected_difficulty') and not self.hit(context, argv.image, 'CBDifficultySelected'):
                return None
            if params.get('require_full_simulation') and detail.get('live_state') not in ('off', 'absent'):
                return None
            target = self.action(context, argv.image, params['action'], observe_only=observed) if 'action' in params else [0, 0, 1, 1]
            if context.tasker.stopping:
                return None
            return self.AnalyzeResult(box=target, detail=detail)
        except Exception:
            return None


def _detail(receipt):
    try:
        detail = receipt['nodes'][-1]['recognition']['best']['detail']
        detail = json.loads(detail) if type(detail) is str else detail
        if type(detail) is not dict:
            raise ValueError()
        return detail
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError('clan_battle_native_detail_unknown') from None


def timing_capability(calibration):
    """Derive timing bounds from a bound measurement file, never a validated flag."""
    binding = calibration.get('timing_measurements')
    unknown = dict(validated=False)
    if type(binding) is not dict or calibration.get('evidence_kind') != 'device_saved_frame':
        return unknown
    path = Path(binding.get('path', ''))
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 1024*1024:
        return unknown
    try:
        data = path.read_bytes()
        if len(data) > 1024*1024 or hashlib.sha256(data).hexdigest() != binding.get('sha256'):
            return unknown
        from .clan_battle_calibration import _json
        measured = _json(data)
    except (OSError, ValueError, UnicodeError):
        return unknown
    if (type(measured) is not dict or measured.get('kind') != 'owner_native_clan_timing'
            or measured.get('calibration_id') != calibration['calibration_id']
            or measured.get('battle_hud') != calibration['battle_hud']
            or measured.get('observation_scope') != 'full_read_tick'
            or measured.get('action_scope') != 'toggle_dispatch_and_full_team_readback'):
        return unknown
    if type(measured.get('battle_speed')) is not int or measured['battle_speed'] != 1:
        return unknown
    values = [measured.get(key) for key in ('observation_intervals_ms', 'action_latencies_ms', 'game_second_ms')]
    if any(type(rows) is not list or len(rows) < 20 or any(type(n) is not int or n <= 0 for n in rows) for rows in values):
        return unknown
    return dict(validated=True, max_observation_interval_ms=max(values[0]),
                max_action_latency_ms=max(values[1]), battle_speed=1, min_game_second_ms=min(values[2]),
                source=str(path), source_sha256=binding['sha256'])


class NativeClanBattle(NativeGuideTrial):
    def __init__(self, tasker, directory, *, calibration=None, battle_speed=1):
        super().__init__(tasker, Path(directory))
        self.calibration = calibration
        self.timing_capability = dict(validated=False)
        self.started = False
        self.battle_speed, self.opening_paused = battle_speed, False

    def configure(self, plan):
        from .clan_battle import load_snapshot, support_requirement
        from .clan_battle_characters import ClanBattleCharacters
        nodes = calibration_pipeline(self.calibration, plan)
        require_battle_speed(self.calibration, plan, self.battle_speed)
        self.plan, self.snapshot = plan, load_snapshot(plan)
        if self.calibration['formation'].get('star_source') == 'account_snapshot':
            from .clan_battle import account_battle_stars
            account_battle_stars(plan)
        self.support_unit_id = support_requirement(plan, self.snapshot)
        self.page = ClanBattlePage(plan, self.calibration, self.support_unit_id)
        self.characters = ClanBattleCharacters(plan, self.calibration, self.page)
        self.directory.mkdir(parents=True, exist_ok=True)
        if (not self.tasker.resource.override_pipeline(game_session_pipeline(int(plan['account_id'])))
                or not self.tasker.resource.override_pipeline(nodes)):
            raise RuntimeError('clan_battle_pipeline_rejected')
        resource = getattr(self.tasker, '_resource_holder', None) or self.tasker.resource
        if not resource.register_custom_recognition('ClanBattlePage', self.page):
            raise RuntimeError('clan_battle_callback_registration_failed:ClanBattlePage')
        if not resource.register_custom_recognition('ClanBattleCharacters', self.characters):
            raise RuntimeError('clan_battle_callback_registration_failed:ClanBattleCharacters')
        # Keep the callbacks with the real Resource; registration rejection is terminal.
        self.timing_capability = timing_capability(self.calibration)

    def choose(self, names, *, timeout=1000):
        try:
            return super().choose(names, timeout=timeout)
        except RuntimeError as exc:
            if str(exc).startswith('guide_trial_disconnected_6002:'):
                raise RuntimeError('clan_battle_disconnected_6002:battle_start=' + self.battle_start) from exc
            raise

    def run(self, node, *, click=False):
        starting = click and node == 'CBStart'
        if starting:
            if self.started:
                raise RuntimeError('clan_battle_start_already_attempted')
            self.started, self.battle_start = True, 'unknown'
        try:
            result = super().run(node, click=click)
        except native.NativeInputNotSent as exc:
            if starting:
                self.battle_start = 'not_sent'
                exc.receipt['clan_battle_phase'] = 'simulation_start'
            raise
        if starting:
            self.battle_start = 'sent'
        self.last_receipt = result
        if any(row.get('name') == 'LabyDisconnect6002Observed' for row in result.get('nodes', [])):
            raise RuntimeError('clan_battle_disconnected_6002:battle_start=' + self.battle_start)
        if click and getattr(getattr(self, 'page', None), 'modern', False):
            action = node.removeprefix('CBAction_') if node.startswith('CBAction_') else (
                'start' if node == 'CBStart' else 'support_back' if node == 'CBAddSupport' else None)
            if action in V2_DESTINATIONS:
                self.page.advance(action)
        return result

    def read_page(self, page):
        receipt = self.run('CBPage_'+page)
        return _detail(receipt), receipt

    def prepare(self, plan):
        if self.calibration is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Exact complete closure text is read-only; unknown/open pages stay blocked.
            if self.choose(['CBClosedReadOnly']) == 'CBClosedReadOnly':
                raise RuntimeError('clan_battle_closed')
        self.configure(plan)
        modern = self.calibration.get('version') == 2
        initial = ['LabyClientGameHomeObserved', 'LabyClientAdventureObserved']
        if modern:
            initial = ['CBObserve_boss_detail', 'CBObserve_lobby', *initial]
        hit = self.wait(initial, seconds=10)
        if hit == 'CBObserve_boss_detail':
            self.run('CBAction_back_to_lobby', click=True)
            self.wait(['CBObserve_lobby'], seconds=10)
            hit = 'CBObserve_lobby'
        if hit == 'CBObserve_lobby':
            self.run('CBAction_back_to_adventure', click=True)
            self.wait(['LabyClientAdventureObserved'], seconds=10)
            self.page.reset_context()
        if hit == 'LabyClientGameHomeObserved':
            self.run('LabyClientHomeToAdventure', click=True)
            self.wait(['LabyClientAdventureObserved'], seconds=10)
        self.run('LabyClientOpenMainMenuOnce', click=True)
        self.wait(['LabyClientMainMenuObserved'], seconds=10)
        self.run('LabyClientOpenProfileOnce', click=True)
        self.wait([PROFILE_NODE], seconds=10)
        self.account_receipt = self.run(PROFILE_NODE)
        self.account = str(profile_player_id(self.account_receipt))
        if self.account != plan['account_id']:
            raise RuntimeError('clan_battle_account_mismatch')
        self.run('LabyClientProfileBackOnce', click=True)
        self.wait(['LabyClientMainMenuObserved'], seconds=10)
        self.run('LabyClientMenuToAdventureOnce', click=True)
        self.wait(['LabyClientAdventureObserved'], seconds=10)
        self.run('CBAction_open_clan', click=True)
        if self.wait(['CBPage_closed', 'CBPage_lobby'], seconds=15) == 'CBPage_closed':
            raise RuntimeError('clan_battle_closed')
        if modern:
            self._open_simulation_target(plan)
            self.run('CBAction_open_formation', click=True)
        else:
            self.read_page('lobby')
            self.run('CBAction_open_boss', click=True)
            self.wait(['CBPage_target'], seconds=10)
            self.read_page('target')
            self.run('CBAction_open_simulation', click=True)
        self.wait(['CBPage_formation'], seconds=10)
        self.form_team()

    def _open_simulation_target(self, plan):
        """Select and read the requested independent simulation from the lobby."""
        _, lobby_receipt = self.read_page('lobby')
        action = self.run('CBAction_open_boss', click=True)
        self.wait(['CBPage_boss_detail'], seconds=10)
        detail, _ = self.read_page('boss_detail')
        phases = ('B', 'C') if plan['phase'] == 'BC' else (plan['phase'],)
        if detail['phase'] not in phases:
            self.run('CBAction_open_difficulty', click=True)
            self.wait(['CBPage_difficulty'], seconds=10)
            self.run('CBAction_select_difficulty', click=True)
            self.wait(['CBDifficultyChosen'], seconds=10)
            self.run('CBAction_confirm_difficulty', click=True)
            self.wait(['CBPage_boss_detail'], seconds=10)
            detail, _ = self.read_page('boss_detail')
            if detail['phase'] not in phases:
                raise RuntimeError('clan_battle_difficulty_selection_unverified')
        if self.choose(['CBPage_target']) != 'CBPage_target':
            self.run('CBAction_open_simulation', click=True)
            self.wait(['CBPage_target'], seconds=10)
        target, receipt = self.read_page('target')
        if target['live_state'] == 'on':
            self.run('CBAction_disable_live_state', click=True)
            self.wait(['CBFullSimulationReady'], seconds=10)
            target, receipt = self.read_page('target')
        if target['live_state'] not in ('off', 'absent'):
            raise RuntimeError('clan_battle_simulation_settings_not_ready')
        return target, dict(lobby=lobby_receipt, select=action, target=receipt)

    def read_team(self):
        deadline = time.monotonic() + 15
        while True:
            reading = _detail(self.run('CBReadTeam'))
            if (not self.calibration or self.calibration.get('version') != 2
                    or sum(type(star) is int for star in reading.get('stars', [])) == reading.get('member_count')):
                return reading
            # Cards alternate star digits and equipment badges; observation must not clear a valid team.
            if time.monotonic() >= deadline:
                raise RuntimeError('clan_battle_stars_unreadable')
            self.checkpoint()
            time.sleep(.05)

    def _list_hit(self, section, position):
        # Read-only recognition is dispatched through the same Owner, never a controller shortcut.
        return self.choose([f'CBList_{section}_{position}']) == f'CBList_{section}_{position}'

    def _reset_list(self, section):
        if not self._list_hit(section, 'top'):
            name = 'characters_top' if section == 'formation' else 'support_top'
            self.run('CBAction_'+name, click=True)
            if not self._list_hit(section, 'top'):
                raise RuntimeError('clan_battle_list_top_not_verified')

    def _next_list(self, section):
        if self._list_hit(section, 'bottom'):
            return False
        before = self.tasker.controller.cached_image.copy()
        name = 'characters_next' if section == 'formation' else 'support_next'
        self.run('CBAction_'+name, click=True)
        self.read_page(section)
        after = self.tasker.controller.cached_image
        rois = self.calibration[section]['candidates' if section == 'formation' else 'slots']
        def digest(frame):
            return [hashlib.sha256(frame[y:y+h, x:x+w].tobytes()).digest() for x,y,w,h in rois]
        if after is None or before.shape != after.shape or digest(before) == digest(after):
            raise RuntimeError('clan_battle_list_unchanged_no_retry')
        return True

    def form_team(self):
        from .clan_battle import select_support
        from .clan_battle_characters import box_inside_roi
        team = self.read_team()
        try:
            self.page.require_team(team)
            return
        except RuntimeError:
            pass
        for _ in range(5):
            if team['member_count'] == 0:
                break
            self.tasker.resource.override_pipeline({'CBEditTeam': dict(custom_recognition_param=dict(mode='clear', count=team['member_count']))})
            self.run('CBEditTeam', click=True)
            after = self.read_team()
            if after['member_count'] != team['member_count']-1:
                raise RuntimeError('clan_battle_clear_readback_unknown')
            team = after
        for uid in self.plan['unit_ids']:
            if uid == self.support_unit_id:
                continue
            self._reset_list('formation')
            found = False
            for _ in range(16):
                self.read_team()
                frame = self.tasker.controller.cached_image
                matches = [(row, roi) for roi in self.calibration['formation']['candidates']
                           if (row := self.characters.match(frame, uid, roi))]
                if len(matches) > 1:
                    raise RuntimeError('clan_battle_candidate_ambiguous')
                if matches:
                    if not box_inside_roi(matches[0][0]['box'], matches[0][1]):
                        raise RuntimeError('clan_battle_candidate_clipped')
                    self.tasker.resource.override_pipeline({'CBEditTeam': dict(custom_recognition_param=dict(mode='add', unit_id=uid))})
                    self.run('CBEditTeam', click=True)
                    after = self.read_team()
                    if after['member_count'] != team['member_count']+1 or uid not in after['unit_ids']:
                        raise RuntimeError('clan_battle_add_readback_unknown')
                    team, found = after, True
                    break
                if not self._next_list('formation'):
                    break
            if not found:
                raise RuntimeError('clan_battle_character_not_found:' + str(uid))
        if self.support_unit_id is not None:
            self.run('CBAction_open_support', click=True)
            self.wait(['CBPage_support'], seconds=10)
            self._reset_list('support')
            selected = None
            for _ in range(16):
                reading = _detail(self.run('CBReadSupport'))
                try:
                    selected = select_support(self.plan, self.snapshot, reading['candidates'])
                except RuntimeError as exc:
                    if str(exc) != 'clan_battle_matching_support_unavailable':
                        raise
                    selected = None
                if selected is not None:
                    self.tasker.resource.override_pipeline({'CBAddSupport': dict(custom_recognition_param=dict(
                        mode='support_add', support_id=selected['support_id'], unit_id=selected['unit_id'], stars=selected['stars']))})
                    self.run('CBAddSupport', click=True)
                    self.wait(['CBPage_formation'], seconds=10)
                    break
                if not self._next_list('support'):
                    break
            if selected is None:
                raise RuntimeError('clan_battle_matching_support_unavailable')
        self.page.require_team(self.read_team())

    def verify_team(self, plan):
        reading = self.read_team()
        self.page.require_team(reading)
        if self.account != plan['account_id']:
            raise RuntimeError('clan_battle_account_mismatch')
        return dict(reading, account_id=self.account, server=self.calibration['server'],
            native_evidence=dict(account=self.account_receipt, formation=reading,
                calibration_id=self.calibration['calibration_id'], evidence_kind=self.calibration['evidence_kind']))

    def read_battle_settings(self, initial=None):
        if initial is None:
            node = 'CBOpeningRead' if self.opening_paused else 'CBReadSettings'
            if self.choose([node]) != node:
                raise RuntimeError('clan_battle_controls_unknown')
            initial = self.last_receipt
        state = _detail(initial)
        self.last_controls = deepcopy(state)
        return state

    def _toggle(self, node, previous, desired, *, slot=None, remaining_seconds=None):
        before = getattr(self, 'last_controls', None)
        state = self.read_battle_settings()
        if remaining_seconds is not None and (type(remaining_seconds) is not int or not 0 <= remaining_seconds <= 89
                or state.get('same_frame') is not True or type(state.get('remaining_seconds')) is not int
                or state['remaining_seconds'] != remaining_seconds
                or self.calibration and self.calibration.get('version') == 2
                and (type(state.get('battle_speed')) is not int or state['battle_speed'] != 1)):
            raise RuntimeError('clan_battle_switch_window_missed')
        if before is not None and any(before.get(key) != state.get(key) for key in ('auto', 'sets', 'unit_ids')):
            raise RuntimeError('clan_battle_switch_state_changed')
        if self.opening_paused and before is not None and before.get('battle_speed') != state.get('battle_speed'):
            raise RuntimeError('clan_battle_switch_state_changed')
        key = 'auto' if slot is None else 'sets'
        value = state['auto'] if slot is None else state['sets'][slot]
        if type(previous) is not bool or type(desired) is not bool or value is not previous or previous is desired:
            raise RuntimeError('clan_battle_switch_previous_unknown')
        expected = dict(auto=state['auto'], sets=list(state['sets']), unit_ids=list(state['unit_ids']))
        if slot is None:
            expected['auto'] = desired
        else:
            expected[key][slot] = desired
        params = dict(mode='battle_auto' if slot is None else 'battle_set', previous=previous,
                      desired=desired, auto=state['auto'], sets=state['sets'], unit_ids=state['unit_ids'])
        if remaining_seconds is not None:
            params['expected_remaining_seconds'] = remaining_seconds
        if slot is not None:
            params['slot'] = slot
        confirmed = 'CBSettingsConfirmed'
        if self.opening_paused:
            if remaining_seconds is not None:
                raise RuntimeError('clan_battle_switch_window_missed')
            node = 'CBOpeningAuto' if slot is None else 'CBOpeningSet'
            params.update(mode='opening_auto' if slot is None else 'opening_set', battle_speed=state['battle_speed'])
            expected['battle_speed'] = state['battle_speed']
            confirmed = 'CBOpeningConfirmed'
        if self.tasker.resource.override_pipeline({node: dict(custom_recognition_param=params),
                confirmed: dict(custom_recognition_param=dict(mode='opening_read' if self.opening_paused else 'battle_read', **expected))}) is False:
            raise RuntimeError('clan_battle_switch_pipeline_rejected')
        reading = _detail(self.run(node, click=True))
        if any(reading.get(name) != expected[name] for name in expected):
            raise RuntimeError('clan_battle_switch_readback_unknown')
        if (remaining_seconds is not None and self.calibration and self.calibration.get('version') == 2
                and (type(reading.get('battle_speed')) is not int or reading['battle_speed'] != 1)):
            raise RuntimeError('clan_battle_speed_unknown')
        self.last_controls = deepcopy(reading)
        return reading

    def toggle_auto(self, previous, desired, *, remaining_seconds=None):
        return self._toggle('CBToggleAuto', previous, desired, remaining_seconds=remaining_seconds)

    def toggle_set(self, slot, previous, desired, *, remaining_seconds=None):
        if type(slot) is not int or not 0 <= slot < 5:
            raise RuntimeError('clan_battle_switch_slot_invalid')
        return self._toggle('CBToggleSet', previous, desired, slot=slot, remaining_seconds=remaining_seconds)

    def read_tick(self):
        self.checkpoint()
        hit = self.choose(['CBPage_result', 'CBReadSettings'])
        if hit == 'CBPage_result':
            receipt = self.last_receipt
            result = _detail(receipt)
            return dict(result=dict(result, result_verified=True, result_evidence=receipt))
        if hit != 'CBReadSettings':
            raise RuntimeError('clan_battle_tick_unknown')
        state = self.read_battle_settings(self.last_receipt)
        if (type(state.get('remaining_seconds')) is not int or not 0 <= state['remaining_seconds'] <= 90
                or state.get('same_frame') is not True or type(state.get('frame_pixel_sha256')) is not str
                or re.fullmatch('[0-9a-f]{64}', state['frame_pixel_sha256']) is None):
            raise RuntimeError('clan_battle_tick_same_frame_unknown')
        if getattr(self, 'plan', {}).get('switches') and (type(state.get('battle_speed')) is not int or state['battle_speed'] != 1):
            raise RuntimeError('clan_battle_speed_unknown')
        return dict(remaining_seconds=state['remaining_seconds'], state=state,
            same_frame=True, frame_pixel_sha256=state['frame_pixel_sha256'])

    def start_and_read(self, plan):
        from .clan_battle import execute_switches, require_timing
        ready = self.verify_team(plan)
        require_timing(self, plan)
        start = self.run('CBStart', click=True)
        if self.calibration and self.calibration.get('auto_battle') and not plan['switches']:
            initial = start
            self.opening_paused = True
        else:
            hit = self.wait(['CBReadSettings', 'CBPage_result'], seconds=30)
            if hit != 'CBReadSettings':
                raise RuntimeError('clan_battle_settings_not_observed')
            initial = self.last_receipt
        settings = execute_switches(self, plan, initial=initial)
        if self.opening_paused:
            state = self.read_battle_settings()
            if any(state.get(key) != settings['opening_evidence'].get(key) for key in ('auto', 'sets', 'unit_ids', 'battle_speed')):
                raise RuntimeError('clan_battle_switch_state_changed')
            for _ in range(2):
                if state['battle_speed'] == self.battle_speed:
                    break
                expected = {key: deepcopy(state[key]) for key in ('auto', 'sets', 'unit_ids', 'battle_speed')}
                desired = {1: 2, 2: 4, 4: 1}[state['battle_speed']]
                if not self.tasker.resource.override_pipeline({
                        'CBOpeningSpeed': dict(custom_recognition_param=dict(mode='opening_speed', **expected)),
                        'CBOpeningConfirmed': dict(custom_recognition_param=dict(mode='opening_read', **dict(expected, battle_speed=desired)))}):
                    raise RuntimeError('clan_battle_switch_pipeline_rejected')
                state = self.read_battle_settings(self.run('CBOpeningSpeed', click=True))
                if any(state.get(key) != value for key, value in dict(expected, battle_speed=desired).items()):
                    raise RuntimeError('clan_battle_speed_readback_unknown')
                settings['actions'].append(dict(control='speed', previous=expected['battle_speed'], target=desired))
            if state['battle_speed'] != self.battle_speed:
                raise RuntimeError('clan_battle_speed_readback_unknown')
            settings['opening_evidence'] = deepcopy(state)
            expected = {key: deepcopy(state[key]) for key in ('auto', 'sets', 'unit_ids', 'battle_speed')}
            if not self.tasker.resource.override_pipeline({'CBResumeOpening': dict(custom_recognition_param=dict(mode='opening_resume', **expected)),
                    'CBSettingsConfirmed': dict(custom_recognition_param=dict(mode='battle_read', **expected))}):
                raise RuntimeError('clan_battle_switch_pipeline_rejected')
            self.run('CBResumeOpening', click=True)
            self.opening_paused = False
            self.wait(['CBSettingsConfirmed'], seconds=15)
            settings['observation'] = self.read_battle_settings(self.last_receipt)
            settings['battle_speed'] = self.battle_speed
        self.wait(['CBPage_result'], seconds=180)
        result, result_receipt = self.read_page('result')
        require_target_identity(plan, result)
        return dict(ready, damage=result['damage'], result_verified=True, settings_verified=True,
            settings_evidence=settings, result_evidence=result_receipt,
            native_evidence=dict(start=start, ready=ready['native_evidence'], result=result_receipt))

    def return_to_target(self, plan):
        before, before_receipt = self.read_page('result')
        require_target_identity(plan, before)
        action = self.run('CBAction_return_to_target', click=True)
        if self.calibration.get('version') == 2:
            self.wait(['CBPage_lobby'], seconds=15)
            target, target_receipt = self._open_simulation_target(plan)
        else:
            self.wait(['CBPage_target'], seconds=15)
            target, target_receipt = self.read_page('target')
        require_target_identity(plan, target)
        if self.account != plan['account_id']:
            raise RuntimeError('clan_battle_account_mismatch')
        return dict(target, account_id=self.account, server=self.calibration['server'],
            return_verified=True, native_evidence=dict(before=before_receipt,
                action=action, target=target_receipt))
