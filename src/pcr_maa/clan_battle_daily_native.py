"""Read real formal-battle counters; missing consumption pages never authorize input."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from maa.custom_recognition import CustomRecognition

from .clan_battle_native import ClanBattlePage, NativeClanBattle, _current_period, _detail, _node, _roi
from .clan_battle_characters import ClanBattleCharacters, box_inside_roi
from .guide_trial_native import NativeGuideTrial


PAGE_FIELDS = {
    'lobby': {'remaining_count', 'carry_count', 'cp'},
    'target': {'remaining_count', 'cp', 'health', 'boss', 'phase'},
    'carry_empty': set(),
}
PATTERNS = {
    'remaining_count': r'^[0-3]$',
    'carry_count': r'^[0-3]$',
    'cp': r'^[0-9]{1,3}\s*/\s*900$',
    'health': r'^[0-9][0-9,，]*\s*/\s*[1-9][0-9,，]*$',
    'phase': r'^第[1-9]阶段$',
    'period': r'^[0-1]?[0-9]/[0-3]?[0-9]\s*[0-2][0-9]:[0-5][0-9]\s*[~～〜至—－-]\s*[0-1]?[0-9]/[0-3]?[0-9]\s*[0-2][0-9]:[0-5][0-9]$',
    'game_day': r'^20[0-9]{2}[-/][0-1][0-9][-/][0-3][0-9]$',
    'damage': r'^[0-9][0-9,，]*$',
    'remaining_seconds': r'^(?:1[:：](?:[0-2][0-9]|30)|0[:：][0-5][0-9])$',
}
EXECUTION_FIELDS = {
    'adventure': set(), 'boss_detail': {'boss', 'phase'}, 'formation': set(), 'support': set(),
    'battle': {'remaining_seconds'}, 'result': {'damage'}, 'used_units': {'game_day'}, 'carry': set(),
}
ACTION_PAGES = dict(open_clan='adventure', open_boss='lobby', open_formal='boss_detail',
    open_formation='target', cancel_formation='formation', back_to_lobby='boss_detail',
    back_to_adventure='lobby', open_support='formation', support_back='support',
    characters_top='formation', characters_next='formation', support_top='support', support_next='support',
    start='formation', open_used='lobby', close_used='used_units', open_carry='lobby', close_carry='carry',
    close_empty_carry='carry_empty', open_carry_for_target='target', select_carry='carry', return_to_lobby='result')
DESTINATIONS = dict(open_clan='lobby', open_boss='boss_detail', open_formal='target', open_formation='formation',
    cancel_formation='boss_detail', back_to_lobby='lobby', back_to_adventure='adventure',
    open_support='support', support_back='formation', start='battle', open_used='used_units', close_used='lobby',
    open_carry='carry', open_carry_for_target='carry', close_carry='lobby', close_empty_carry='lobby',
    select_carry='formation', return_to_lobby='lobby')


def validate_daily_calibration(calibration):
    daily = calibration.get('daily')
    if (calibration.get('version') != 2 or type(daily) is not dict
            or set(daily) != ({'version', 'cp_semantics', 'pages', 'samples', 'execution'}
                if daily.get('version') == 2 else {'version', 'cp_semantics', 'pages', 'samples'})
            or type(daily['version']) is not int or daily['version'] not in (1, 2)
            or daily['cp_semantics'] != 'daily_earned'
            or type(daily['pages']) is not dict or set(daily['pages']) != set(PAGE_FIELDS)):
        raise ValueError('clan_battle_daily_calibration_invalid')
    covered = set()
    if type(daily['samples']) is not list or not daily['samples']:
        raise ValueError('clan_battle_daily_samples_missing')
    for sample in daily['samples']:
        if (type(sample) is not dict or set(sample) != {'page', 'path', 'sha256'}
                or sample['page'] not in PAGE_FIELDS or type(sample['path']) is not str):
            raise ValueError('clan_battle_daily_sample_invalid')
        path = Path(sample['path'])
        if (not path.is_absolute() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']):
            raise ValueError('clan_battle_daily_sample_changed')
        covered.add(sample['page'])
    if covered != set(PAGE_FIELDS):
        raise ValueError('clan_battle_daily_samples_missing')
    for page, spec in daily['pages'].items():
        fields = PAGE_FIELDS[page] | ({'period', 'phase'} if page == 'lobby' and daily['version'] == 2 else set())
        if (type(spec) is not dict or set(spec) != {'labels', 'rejects', 'fields'}
                or type(spec['labels']) is not list or len(spec['labels']) < 2
                or type(spec['rejects']) is not list
                or type(spec['fields']) is not dict or set(spec['fields']) != fields):
            raise ValueError('clan_battle_daily_page_invalid:' + page)
    return daily


def daily_pipeline(calibration, plan):
    daily = validate_daily_calibration(calibration)
    nodes = {}
    pages = dict(daily['pages'])
    if daily['version'] == 2:
        pages.update(daily['execution']['pages'])
    for page, spec in pages.items():
        for kind in ('labels', 'rejects'):
            for index, label in enumerate(spec[kind]):
                nodes[f'CBDaily_{page}_{kind}_{index}'] = _node(label)
        for field, roi in spec['fields'].items():
            expected = PATTERNS.get(field)
            if field == 'boss':
                expected = '^' + re.escape(calibration['boss_targets'][str(plan['boss'])]['name']) + '$'
            nodes[f'CBDaily_{page}_{field}'] = _node({'roi': roi}, expected=expected)
        nodes['CBDailyPage_' + page] = dict(recognition='Custom', custom_recognition='ClanBattleDailyPage',
            custom_recognition_param=dict(page=page), action='DoNothing', next=[], timeout=1000,
            pre_delay=0, post_delay=0)
    return nodes


class ClanBattleDailyPage(CustomRecognition):
    hit = ClanBattlePage.hit
    word = ClanBattlePage.word

    def __init__(self, calibration, plan):
        super().__init__()
        self.calibration, self.plan = calibration, plan

    def read(self, context, image, page):
        pages = dict(self.calibration['daily']['pages'])
        pages.update(self.calibration['daily'].get('execution', {}).get('pages', {}))
        if (context.tasker.stopping or image is None or image.shape != (720, 1280, 3)
                or page not in pages):
            raise RuntimeError('clan_battle_daily_page_unknown')
        if self.hit(context, image, 'LabyDisconnect6002Observed'):
            raise RuntimeError('clan_battle_disconnected_6002')
        spec = pages[page]
        if (any(self.hit(context, image, f'CBDaily_{page}_rejects_{i}') for i in range(len(spec['rejects'])))
                or not all(self.hit(context, image, f'CBDaily_{page}_labels_{i}') for i in range(len(spec['labels'])))):
            raise RuntimeError('clan_battle_daily_page_unknown:' + page)
        values = {'page': page}
        for field in spec['fields']:
            text = self.word(context, image, f'CBDaily_{page}_{field}')
            if field == 'boss':
                values[field] = self.plan['boss']
            elif field == 'phase':
                values['phase_label'] = text
                values[field] = self.calibration['phase_labels'].get(text)
            elif field == 'health':
                current, maximum = [int(re.sub('[,，\\s]', '', value)) for value in text.split('/')]
                if not 0 <= current <= maximum:
                    raise RuntimeError('clan_battle_daily_health_invalid')
                values.update(health=current, health_max=maximum, alive=current > 0)
            elif field == 'cp':
                values['cp'] = int(text.split('/')[0].strip())
                if not 0 <= values['cp'] <= 900:
                    raise RuntimeError('clan_battle_daily_cp_invalid')
                values['cp_semantics'] = 'daily_earned'
            elif field == 'period':
                values[field] = _current_period(text)
            elif field == 'game_day':
                values[field] = text.replace('/', '-')
            elif field == 'remaining_seconds':
                minute, second = map(int, text.replace('：', ':').split(':'))
                values[field] = minute * 60 + second
            elif field == 'damage':
                values[field] = int(re.sub('[,，]', '', text))
            else:
                values[field] = int(text)
        if page == 'carry_empty':
            values['carry_stock_indices'] = []
        if page == 'target':
            values['formal'] = True
        values['evidence'] = dict(calibration_id=self.calibration['calibration_id'], page=page,
            frame_pixel_sha256=hashlib.sha256(image.tobytes()).hexdigest())
        return values

    def analyze(self, context, argv):
        try:
            params = json.loads(argv.custom_recognition_param)
            if type(params) is not dict or set(params) != {'page'}:
                return None
            values = self.read(context, argv.image, params['page'])
            return self.AnalyzeResult(box=[0, 0, 1, 1], detail=values)
        except Exception:
            return None


class NativeClanDailyPages(NativeGuideTrial):
    """Owner-scoped read-only probe; this class has no battle-start operation."""
    def __init__(self, tasker, directory, *, calibration, plan):
        super().__init__(tasker, Path(directory))
        self.calibration = deepcopy(calibration)
        self.plan = deepcopy(plan)
        nodes = daily_pipeline(self.calibration, self.plan)
        self.page = ClanBattleDailyPage(self.calibration, self.plan)
        self.directory.mkdir(parents=True, exist_ok=True)
        if not tasker.resource.override_pipeline(nodes):
            raise RuntimeError('clan_battle_daily_pipeline_rejected')
        owner = getattr(tasker, '_resource_holder', None) or tasker.resource
        if not owner.register_custom_recognition('ClanBattleDailyPage', self.page):
            raise RuntimeError('clan_battle_callback_registration_failed:ClanBattleDailyPage')

    def read_page(self, page):
        if page not in PAGE_FIELDS:
            raise ValueError('clan_battle_daily_page_invalid')
        receipt = self.run('CBDailyPage_' + page)
        return _detail(receipt), receipt


def require_daily_execution(calibration, plan):
    """A read-only daily profile never qualifies as an executable formal profile."""
    daily = validate_daily_calibration(calibration)
    execution = daily.get('execution')
    keys = {'pages', 'actions', 'used_units', 'carry_slots', 'boss_slots', 'formation_owner', 'formation_mode', 'speed_one', 'samples'}
    if daily['version'] != 2 or type(execution) is not dict or set(execution) != keys:
        raise ValueError('clan_battle_daily_execution_calibration_missing')
    if type(execution['pages']) is not dict or set(execution['pages']) != set(EXECUTION_FIELDS):
        raise ValueError('clan_battle_daily_execution_pages_missing')
    for page, fields in EXECUTION_FIELDS.items():
        spec = execution['pages'][page]
        if (type(spec) is not dict or set(spec) != {'labels', 'rejects', 'fields'}
                or type(spec['labels']) is not list or len(spec['labels']) < 2
                or type(spec['rejects']) is not list or not spec['rejects']
                or type(spec['fields']) is not dict or set(spec['fields']) != fields):
            raise ValueError('clan_battle_daily_execution_page_invalid:' + page)
    samples = execution['samples']
    if type(samples) is not list or {row.get('page') for row in samples if type(row) is dict} != set(EXECUTION_FIELDS):
        raise ValueError('clan_battle_daily_execution_samples_missing')
    for sample in samples:
        if type(sample) is not dict or set(sample) != {'page', 'path', 'sha256'}:
            raise ValueError('clan_battle_daily_execution_sample_invalid')
        path = Path(sample['path'])
        if not path.is_absolute() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']:
            raise ValueError('clan_battle_daily_execution_sample_changed')
    actions = execution['actions']
    if type(actions) is not dict or set(actions) != set(ACTION_PAGES):
        raise ValueError('clan_battle_daily_execution_actions_missing')
    nodes = daily_pipeline(calibration, plan)
    for name, page in ACTION_PAGES.items():
        spec = actions[name]
        scrolling = name in ('characters_top', 'characters_next', 'support_top', 'support_next')
        keys = {'page', 'label', 'target'} | ({'begin', 'end'} if scrolling else set())
        if (type(spec) is not dict or set(spec) != keys or spec['page'] != page):
            raise ValueError('clan_battle_daily_action_invalid:' + name)
        nodes['CBDailyActionLabel_' + name] = _node(spec['label'])
        _roi(spec['target'])
        if name == 'start':
            continue
        nodes['CBAction_' + name] = dict(recognition='Custom', custom_recognition='ClanBattleDailyPage',
            custom_recognition_param=dict(page=page, action=name), action='Click', target=True,
            next=[], max_hit=1, timeout=1000, pre_delay=0, post_delay=0)
        if scrolling:
            nodes['CBAction_' + name].update(action='Swipe', begin=_roi(spec['begin']), end=_roi(spec['end']), duration=300)
    for page in {*PAGE_FIELDS, *EXECUTION_FIELDS}:
        nodes['CBPage_' + page] = dict(nodes['CBDailyPage_' + page])
    if type(execution['boss_slots']) is not dict or set(execution['boss_slots']) != set('12345'):
        raise ValueError('clan_battle_daily_boss_slots_missing')
    for boss, spec in execution['boss_slots'].items():
        if type(spec) is not dict or set(spec) != {'round', 'defeated'}:
            raise ValueError('clan_battle_daily_boss_slot_invalid')
        nodes['CBDailyBossRound' + boss] = _node({'roi': spec['round']}, expected=r'^第\s*[1-9][0-9]*\s*轮$')
        nodes['CBDailyBossDefeated' + boss] = _node(spec['defeated'])
    def members(layout, prefix, size):
        if (type(layout) is not dict or set(layout) != {'slots', 'empty', 'borrowed'}
                or any(type(layout[key]) is not list or len(layout[key]) != size for key in layout)):
            raise ValueError('clan_battle_daily_unit_layout_invalid:' + prefix)
        for index in range(size):
            _roi(layout['slots'][index])
            nodes[f'{prefix}Empty{index}'] = _node(layout['empty'][index])
            nodes[f'{prefix}Borrowed{index}'] = _node(layout['borrowed'][index])
    members(execution['used_units'], 'CBDailyUsed', 15)
    stocks = execution['carry_slots']
    if type(stocks) is not list or not 1 <= len(stocks) <= 3:
        raise ValueError('clan_battle_daily_carry_slots_missing')
    for index, spec in enumerate(stocks):
        if (type(spec) is not dict or set(spec) != {'empty', 'occupied', 'stock_index', 'time', 'members', 'support_owner', 'target'}):
            raise ValueError('clan_battle_daily_carry_slot_invalid')
        prefix = f'CBDailyCarry{index}'
        nodes[prefix + 'Empty'] = _node(spec['empty'])
        nodes[prefix + 'Occupied'] = _node(spec['occupied'])
        nodes[prefix + 'Index'] = _node({'roi': spec['stock_index']}, expected=r'^[0-9]{1,6}$')
        nodes[prefix + 'Time'] = _node({'roi': spec['time']}, expected=r'^(?:[1-9]|[1-8][0-9]|90)$')
        nodes[prefix + 'Owner'] = _node({'roi': spec['support_owner']}, expected=r'^[1-9][0-9]{0,19}$')
        members(spec['members'], prefix, 5)
        _roi(spec['target'])
    nodes['CBDailyFormationOwner'] = _node({'roi': execution['formation_owner']}, expected=r'^[1-9][0-9]{0,19}$')
    mode = execution['formation_mode']
    if type(mode) is not dict or set(mode) != {'regular', 'carryover', 'stock_index', 'time'}:
        raise ValueError('clan_battle_daily_formation_mode_missing')
    nodes['CBDailyFormationRegular'] = _node(mode['regular'])
    nodes['CBDailyFormationCarryover'] = _node(mode['carryover'])
    nodes['CBDailyFormationStock'] = _node({'roi': mode['stock_index']}, expected=r'^[0-9]{1,6}$')
    nodes['CBDailyFormationTime'] = _node({'roi': mode['time']}, expected=r'^(?:[1-9]|[1-8][0-9]|90)$')
    nodes['CBDailyOneSpeed'] = _node(execution['speed_one'])
    return nodes


class ClanBattleDailyExecutionPage(ClanBattleDailyPage):
    def __init__(self, calibration, plan, port):
        super().__init__(calibration, plan)
        self.port = port
        self.current = None
        self.expected = None
        self.bound = None

    def members(self, context, image, layout, prefix, *, complete=False):
        units, support = [], None
        for index, roi in enumerate(layout['slots']):
            empty = self.hit(context, image, f'{prefix}Empty{index}')
            hits = [uid for uid in self.port.all_units
                if (match := self.port.catalogue.match(image, uid, roi)) and box_inside_roi(match['box'], roi)]
            borrowed = self.hit(context, image, f'{prefix}Borrowed{index}')
            if empty:
                if hits or borrowed or complete:
                    raise RuntimeError('clan_battle_daily_units_unknown')
                continue
            if len(hits) != 1:
                raise RuntimeError('clan_battle_daily_units_unknown')
            if borrowed:
                if complete and support is not None:
                    raise RuntimeError('clan_battle_daily_support_ambiguous')
                support = hits[0]
                if complete:
                    units.append(hits[0])
            else:
                units.append(hits[0])
        if len(units) != len(set(units)) or complete and len(units) != 5:
            raise RuntimeError('clan_battle_daily_units_unknown')
        return units, support

    def read(self, context, image, page):
        value = super().read(context, image, page)
        execution = self.calibration['daily']['execution']
        if page == 'lobby':
            if value['period'] != self.plan['period'] or value['phase'] is None:
                raise RuntimeError('clan_battle_daily_period_or_phase_unknown')
            bosses = []
            for boss in range(1, 6):
                dead = self.hit(context, image, 'CBDailyBossDefeated' + str(boss))
                round_ = None if dead else int(re.sub(r'\D', '', self.word(context, image, 'CBDailyBossRound' + str(boss))))
                bosses.append(dict(boss=boss, round=round_, phase=value['phase'], available=not dead))
            value['bosses'] = bosses
        elif page == 'used_units':
            value['used_unit'], _ = self.members(context, image, execution['used_units'], 'CBDailyUsed')
        elif page == 'carry':
            value['stocks'] = []
            for index, spec in enumerate(execution['carry_slots']):
                prefix = f'CBDailyCarry{index}'
                empty, occupied = (self.hit(context, image, prefix + key) for key in ('Empty', 'Occupied'))
                if empty == occupied:
                    raise RuntimeError('clan_battle_daily_carry_slot_unknown')
                if empty:
                    continue
                units, support = self.members(context, image, spec['members'], prefix, complete=True)
                value['stocks'].append(dict(stock_index=int(self.word(context, image, prefix + 'Index')),
                    time=int(self.word(context, image, prefix + 'Time')), using_unit=units, support_unit=support,
                    support_owner_viewer_id=self.word(context, image, prefix + 'Owner') if support else None,
                    target=spec['target']))
            if not value['stocks'] or len({row['stock_index'] for row in value['stocks']}) != len(value['stocks']):
                raise RuntimeError('clan_battle_daily_carry_inventory_unknown')
        elif page in ('target', 'formation', 'support', 'battle', 'result'):
            finished = (page == 'result' and self.current == 'battle' and self.port.pending is not None
                and self.port.started and self.port.battle_start == 'sent')
            if not self.bound or not (page in (self.current, self.expected) or finished):
                raise RuntimeError('clan_battle_daily_transition_unverified')
            if page == 'target' and (value['boss'] != self.bound['boss'] or value['phase'] != self.bound['phase'] or not value['alive']):
                raise RuntimeError('clan_battle_daily_target_changed')
            value.update(self.bound)
            if page == 'formation':
                regular = self.hit(context, image, 'CBDailyFormationRegular')
                carryover = self.hit(context, image, 'CBDailyFormationCarryover')
                if regular == carryover or ('regular' if regular else 'carryover') != self.port.action['kind']:
                    raise RuntimeError('clan_battle_daily_consumption_mode_unknown')
                value['battle_kind'] = self.port.action['kind']
                if carryover:
                    stock = int(self.word(context, image, 'CBDailyFormationStock'))
                    seconds = int(self.word(context, image, 'CBDailyFormationTime'))
                    if stock != self.port.action['stock_index'] or seconds != self.port.action['start_seconds']:
                        raise RuntimeError('clan_battle_daily_selected_carry_changed')
                value['support_owner_viewer_id'] = None
            if page == 'battle':
                value['battle_speed'] = 1 if self.hit(context, image, 'CBDailyOneSpeed') else None
        self.current, self.expected = page, None
        return value

    def require_team(self, reading):
        plan, bound = self.plan, self.bound
        if (not bound or any(reading.get(key) != value for key, value in bound.items())
                or reading.get('battle_kind') != self.port.action['kind']
                or reading.get('member_count') != 5 or set(reading.get('unit_ids', [])) != set(plan['unit_ids'])
                or dict(zip(reading.get('unit_ids', []), reading.get('stars', []))) != dict(zip(plan['unit_ids'], plan['stars']))
                or reading.get('support_unit_id') != self.port.support_unit_id
                or self.port.support_unit_id is not None and (not reading.get('support_owner_viewer_id')
                    or reading['support_owner_viewer_id'] == plan['account_id'])):
            raise RuntimeError('clan_battle_daily_team_or_mode_unknown')

    def action(self, context, image, name):
        spec = self.calibration['daily']['execution']['actions'][name]
        value = self.read(context, image, spec['page'])
        if not self.hit(context, image, 'CBDailyActionLabel_' + name):
            raise RuntimeError('clan_battle_daily_action_unknown:' + name)
        if name == 'open_boss':
            from .clan_battle_batch import _hash
            action = self.port.action
            actual = next(row for row in value['bosses'] if row['boss'] == self.plan['boss'])
            if (not actual['available'] or any(actual[key] != action[key] for key in ('boss', 'phase', 'round'))
                    or _hash(dict(period=value['period'], boss=actual['boss'], phase=actual['phase'], round=actual['round'])) != action['instance_id']):
                raise RuntimeError('clan_battle_daily_target_changed_before_selection')
            return self.calibration['boss_targets'][str(self.plan['boss'])]['target']
        if name == 'select_carry':
            rows = [row for row in value['stocks'] if row['stock_index'] == self.port.action['stock_index']]
            if len(rows) != 1 or rows[0]['time'] != self.port.action['start_seconds']:
                raise RuntimeError('clan_battle_daily_carry_changed')
            return rows[0]['target']
        if name == 'start':
            from .clan_battle_batch import _timestamp
            from .game_day import current_game_day
            pending = self.port.pending
            now = self.port.now()
            if (pending is None or self.port.action != pending['action']
                    or current_game_day(now) != pending['game_day']
                    or not 0 <= (now - _timestamp(pending['observed_at'])).total_seconds() <= 60):
                raise RuntimeError('clan_battle_daily_final_start_binding_changed')
            team = self.port.characters.formation(context, image)
            self.require_team(team)
            ready = pending['ready']
            if (team['unit_ids'] != ready['unit_ids'] or team['stars'] != ready['stars']
                    or team['support_unit_id'] != ready['support_unit']
                    or team['support_owner_viewer_id'] != ready['support_owner_viewer_id']):
                raise RuntimeError('clan_battle_daily_final_team_changed')
        return spec['target']

    def analyze(self, context, argv):
        try:
            params = json.loads(argv.custom_recognition_param)
            if set(params) - {'page', 'action'}:
                return None
            value = self.read(context, argv.image, params['page'])
            target = self.action(context, argv.image, params['action']) if 'action' in params else [0, 0, 1, 1]
            if context.tasker.stopping:
                return None
            return self.AnalyzeResult(box=target, detail=value)
        except Exception:
            return None


class _DailyTeamReader(ClanBattleCharacters):
    def formation(self, context, image):
        reading = super().formation(context, image)
        reading['support_owner_viewer_id'] = (self.page.word(context, image, 'CBDailyFormationOwner')
            if reading['support_unit_id'] is not None else None)
        return reading


class _DailyCharacters(CustomRecognition):
    """One registered callback stays owned while the reviewed slot changes."""
    def __init__(self, port):
        super().__init__()
        self.port = port

    def analyze(self, context, argv):
        return self.port.characters.analyze(context, argv)


class NativeClanDailyAdapter(NativeClanBattle):
    def __init__(self, tasker, directory, *, request, calibrations):
        from .clan_battle_characters import ClanBattleCharacters
        super().__init__(tasker, directory)
        self.request, self.calibrations = deepcopy(request), deepcopy(calibrations)
        self.action, self.pending, self.last_bosses, self.stocks, self.boss_history = None, None, {}, [], {}
        self.all_units = sorted({uid for job in request['slot_plans'].values() for uid in job['unit_ids']})
        self.page, self.characters = None, None
        self.configure_slot(request['slot_plans']['1'])
        catalogue_plan = dict(self.plan, unit_ids=self.all_units)
        self.catalogue = ClanBattleCharacters(catalogue_plan, self.calibration, self.page)
        self.callback = _DailyCharacters(self)
        owner = getattr(tasker, '_resource_holder', None) or tasker.resource
        if not owner.register_custom_recognition('ClanBattleDailyPage', self.page):
            raise RuntimeError('clan_battle_callback_registration_failed:ClanBattleDailyPage')
        if not owner.register_custom_recognition('ClanBattleCharacters', self.callback):
            raise RuntimeError('clan_battle_callback_registration_failed:ClanBattleCharacters')

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def configure_slot(self, plan, *, capability='formal'):
        from .clan_battle import load_snapshot, support_requirement
        from .clan_battle_calibration import load_calibration
        from .clan_battle_characters import ClanBattleCharacters
        from .clan_battle_native import calibration_pipeline, timing_capability
        from .labyrinth.game_session import game_session_pipeline
        calibration = (load_calibration(plan, capability='carryover') if capability == 'carryover'
            else self.calibrations[plan['confirmed_for']])
        nodes = calibration_pipeline(calibration, plan)
        nodes.update(require_daily_execution(calibration, plan))
        self.plan, self.calibration, self.snapshot = plan, calibration, load_snapshot(plan)
        self.support_unit_id = support_requirement(plan, self.snapshot)
        if self.page is None:
            self.page = ClanBattleDailyExecutionPage(calibration, plan, self)
        else:
            self.page.calibration, self.page.plan = calibration, plan
        self.characters = _DailyTeamReader(plan, calibration, self.page)
        self.timing_capability = timing_capability(calibration)
        self.directory.mkdir(parents=True, exist_ok=True)
        if (not self.tasker.resource.override_pipeline(game_session_pipeline(int(plan['account_id'])))
                or not self.tasker.resource.override_pipeline(nodes)):
            raise RuntimeError('clan_battle_daily_pipeline_rejected')

    def run(self, node, *, click=False):
        from .native import NativeInputNotSent
        starting = click and node == 'CBStart'
        if starting:
            if self.started or self.pending is None:
                raise RuntimeError('clan_battle_daily_start_not_reserved')
            self.started, self.battle_start = True, 'unknown'
        try:
            receipt = NativeGuideTrial.run(self, node, click=click)
        except NativeInputNotSent as exc:
            if starting:
                self.battle_start = 'not_sent'
                exc.receipt.update(clan_battle_phase=self.action['kind'] + '_start', daily_intent_id=self.pending['intent_id'])
            raise
        self.last_receipt = receipt
        if starting:
            self.battle_start = 'sent'
        if click:
            action = node.removeprefix('CBAction_') if node.startswith('CBAction_') else (
                'start' if node == 'CBStart' else 'support_back' if node == 'CBAddSupport' else None)
            if action in DESTINATIONS:
                self.page.expected = DESTINATIONS[action]
        return receipt

    def enter_lobby(self):
        names = ['CBPage_lobby', 'CBPage_boss_detail', 'CBPage_formation', 'CBPage_support',
            'CBPage_adventure', 'LabyClientGameHomeObserved']
        hit = self.wait(names, seconds=10)
        if hit == 'CBPage_support':
            self.run('CBAction_support_back', click=True)
            self.wait(['CBPage_formation'], seconds=10)
            hit = 'CBPage_formation'
        if hit == 'CBPage_formation':
            self.run('CBAction_cancel_formation', click=True)
            self.wait(['CBPage_boss_detail'], seconds=10)
            hit = 'CBPage_boss_detail'
        if hit == 'CBPage_boss_detail':
            self.run('CBAction_back_to_lobby', click=True)
            self.wait(['CBPage_lobby'], seconds=10)
            hit = 'CBPage_lobby'
        if hit == 'LabyClientGameHomeObserved':
            self.run('LabyClientHomeToAdventure', click=True)
            self.wait(['CBPage_adventure'], seconds=10)
            hit = 'CBPage_adventure'
        if self.account is None:
            from .labyrinth.profile import PROFILE_NODE, profile_player_id
            if hit == 'CBPage_lobby':
                self.run('CBAction_back_to_adventure', click=True)
                self.wait(['CBPage_adventure'], seconds=10)
            self.run('LabyClientOpenMainMenuOnce', click=True)
            self.wait(['LabyClientMainMenuObserved'], seconds=10)
            self.run('LabyClientOpenProfileOnce', click=True)
            self.wait([PROFILE_NODE], seconds=10)
            self.account_receipt = self.run(PROFILE_NODE)
            self.account = str(profile_player_id(self.account_receipt))
            if self.account != self.plan['account_id']:
                raise RuntimeError('clan_battle_account_mismatch')
            self.run('LabyClientProfileBackOnce', click=True)
            self.wait(['LabyClientMainMenuObserved'], seconds=10)
            self.run('LabyClientMenuToAdventureOnce', click=True)
            self.wait(['CBPage_adventure'], seconds=10)
            hit = 'CBPage_adventure'
        if hit == 'CBPage_adventure':
            self.run('CBAction_open_clan', click=True)
            self.wait(['CBPage_lobby'], seconds=10)

    def inventory(self):
        self.run('CBAction_open_carry', click=True)
        hit = self.wait(['CBPage_carry_empty', 'CBPage_carry'], seconds=10)
        reading, receipt = self.read_page('carry_empty' if hit == 'CBPage_carry_empty' else 'carry')
        self.stock_observed_at = self.now().isoformat()
        stocks = [] if hit == 'CBPage_carry_empty' else reading['stocks']
        self.run('CBAction_close_empty_carry' if hit == 'CBPage_carry_empty' else 'CBAction_close_carry', click=True)
        self.wait(['CBPage_lobby'], seconds=10)
        return stocks, receipt

    def observe(self, binding):
        from .clan_battle_batch import _hash
        self.enter_lobby()
        self.run('CBAction_open_used', click=True)
        self.wait(['CBPage_used_units'], seconds=10)
        used, used_receipt = self.read_page('used_units')
        self.run('CBAction_close_used', click=True)
        self.wait(['CBPage_lobby'], seconds=10)
        self.stocks, stock_receipt = self.inventory()
        lobby, lobby_receipt = self.read_page('lobby')
        if used['game_day'] != binding['game_day'] or lobby['carry_count'] != len(self.stocks):
            raise RuntimeError('clan_battle_daily_day_or_inventory_changed')
        stamp, bosses = self.now().isoformat(), []
        for value in lobby['bosses']:
            row = dict(value, instance_id=None, appeared_at=None, evidence=dict(lobby=lobby_receipt))
            previous = self.boss_history.get(row['boss'])
            if row['available']:
                row['instance_id'] = _hash(dict(period=lobby['period'], boss=row['boss'], phase=row['phase'], round=row['round']))
                if previous and previous['instance_id'] == row['instance_id']:
                    row['appeared_at'] = previous['appeared_at']
                elif previous and previous['round'] is not None and row['round'] > previous['round']:
                    row['appeared_at'] = stamp
                    row['evidence']['previous'] = previous['evidence']
                self.boss_history[row['boss']] = deepcopy(row)
            bosses.append(row)
        self.last_bosses = {row['boss']: deepcopy(row) for row in bosses}
        return dict(binding, observed_at=stamp, remaining_count=lobby['remaining_count'], cp=lobby['cp'],
            cp_semantics='daily_earned', cp_evidence=lobby_receipt, used_unit=used['used_unit'],
            carry_stock_indices=[row['stock_index'] for row in self.stocks], bosses=bosses,
            reset_verified=True, reset_evidence=dict(personal_record=used_receipt, inventory=stock_receipt, account=self.account_receipt))

    def prepare(self, action, job, binding):
        if self.pending is not None:
            raise RuntimeError('clan_battle_daily_previous_input_unresolved')
        self.enter_lobby()
        self.configure_slot(job, capability='carryover' if action['kind'] == 'carryover' else 'formal')
        self.action, self.start_seconds = deepcopy(action), action['start_seconds']
        actual = self.last_bosses.get(action['boss'])
        if not actual or any(actual[key] != action[key] for key in ('boss', 'round', 'phase', 'instance_id')):
            raise RuntimeError('clan_battle_daily_target_changed')
        self.page.bound = dict(period=job['period'], formal=True,
            **{key: action[key] for key in ('boss', 'round', 'phase', 'instance_id')})
        self.run('CBAction_open_boss', click=True)
        self.wait(['CBPage_boss_detail'], seconds=10)
        self.run('CBAction_open_formal', click=True)
        self.wait(['CBPage_target'], seconds=10)
        if action['kind'] == 'regular':
            self.run('CBAction_open_formation', click=True)
            self.wait(['CBPage_formation'], seconds=10)
            self.form_team()
        else:
            self.run('CBAction_open_carry_for_target', click=True)
            self.wait(['CBPage_carry'], seconds=10)
            self.run('CBAction_select_carry', click=True)
            self.wait(['CBPage_formation'], seconds=10)
        team = self.read_team()
        self.page.require_team(team)
        return dict(binding, **action, slot_confirmed_for=job['confirmed_for'], verified=True,
            unit_ids=team['unit_ids'], stars=team['stars'], support_unit=team['support_unit_id'],
            support_owner_viewer_id=team.get('support_owner_viewer_id'),
            evidence=dict(account=self.account_receipt, formation=team, mode='formal', calibration_id=self.calibration['calibration_id']))

    def execute(self, intent, job):
        from .clan_battle import execute_switches, require_timing
        from .clan_battle_batch import BINDING_KEYS
        from .game_day import current_game_day
        from .supported_daily import _atomic_json
        if (self.pending is not None or self.action != intent['action']
                or current_game_day(self.now()) != intent['game_day']):
            raise RuntimeError('clan_battle_daily_start_binding_changed')
        if self.action['kind'] == 'carryover':
            from .clan_battle_calibration import load_calibration
            require_daily_execution(load_calibration(job, capability='carryover'), job)
        require_timing(self, job)
        team = self.read_team()
        self.page.require_team(team)
        if (team['unit_ids'] != intent['ready']['unit_ids'] or team['stars'] != intent['ready']['stars']
                or team['support_unit_id'] != intent['ready']['support_unit']
                or team.get('support_owner_viewer_id') != intent['ready']['support_owner_viewer_id']):
            raise RuntimeError('clan_battle_daily_team_changed_before_start')
        self.pending, self.started = deepcopy(intent), False
        start = self.run('CBStart', click=True)
        if self.wait(['CBReadSettings', 'CBPage_result'], seconds=30) != 'CBReadSettings':
            raise RuntimeError('clan_battle_settings_not_observed')
        settings = execute_switches(self, job, initial=self.last_receipt)
        self.wait(['CBPage_result'], seconds=180)
        result, result_receipt = self.read_page('result')
        # Preserve known damage before any return/navigation, even if consumption remains unresolved.
        _atomic_json(self.directory / ('result-' + intent['intent_id'] + '.json'), dict(intent_id=intent['intent_id'],
            damage=result['damage'], result_evidence=result_receipt, settings_evidence=settings, start=start))
        self.run('CBAction_return_to_lobby', click=True)
        self.wait(['CBPage_lobby'], seconds=15)
        binding = {key: intent[key] for key in BINDING_KEYS}
        after = self.observe(binding)
        regular = intent['action']['kind'] == 'regular'
        earned = after['cp'] // 300 - intent['before']['cp'] // 300
        if (after['cp'] < intent['before']['cp']
                or intent['before']['remaining_count'] + earned - after['remaining_count'] != int(regular)):
            raise RuntimeError('clan_battle_daily_consumption_unknown')
        before_stock, after_stock = set(intent['before']['carry_stock_indices']), set(after['carry_stock_indices'])
        added, removed = after_stock - before_stock, before_stock - after_stock
        if (regular and (removed or len(added) > 1)
                or not regular and (added or removed != {intent['action']['stock_index']})):
            raise RuntimeError('clan_battle_daily_carry_consumption_unknown')
        grant = None
        if added:
            stock = next(row for row in self.stocks if row['stock_index'] in added)
            ready = intent['ready']
            if (stock['using_unit'] != ready['unit_ids'] or stock['support_unit'] != ready['support_unit']
                    or stock['support_owner_viewer_id'] != ready['support_owner_viewer_id']):
                raise RuntimeError('clan_battle_daily_grant_team_unknown')
            grant = dict(binding, slot_confirmed_for=ready['slot_confirmed_for'], source_slot=ready['slot'],
                source_intent_id=intent['intent_id'], stock_index=stock['stock_index'], time=stock['time'],
                using_unit=stock['using_unit'], support_unit=stock['support_unit'],
                support_owner_viewer_id=stock['support_owner_viewer_id'], source_instance_id=ready['instance_id'],
                issued_at=self.stock_observed_at, evidence=dict(inventory=after['reset_evidence']['inventory'],
                    issuance_bound='new_stock_readback_after_reserved_battle'))
        self.pending = None
        return dict(ready=deepcopy(intent['ready']), result_verified=True, consumption_verified=True,
            damage=result['damage'], settings_verified=True, settings_evidence=settings,
            result_evidence=result_receipt, grant=grant, after=after, return_verified=True)
