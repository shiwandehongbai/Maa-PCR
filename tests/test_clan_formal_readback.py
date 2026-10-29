"""Real counter/health frames exercise only the existing read-only recognizer.

These partial page fixtures do not qualify a formal execution calibration.
"""
from copy import deepcopy
import hashlib

import pytest

from pcr_maa import native
from pcr_maa.clan_battle_calibration import load_calibration
from pcr_maa.clan_battle_daily_native import ClanBattleDailyPage, PATTERNS
from pcr_maa.clan_battle_native import _node, _detail
from pcr_maa.labyrinth.game_session import game_session_pipeline
from pcr_maa.replay import SavedImages
from test_clan_opening import sampled_plan


SAMPLES = {
    'lobby':'2ccc85043a2ce1df0c5ccebd5f544d14a7f7c4902d81ba6d861766b7a378b6fa',
    'boss3':'2b118db95525663f7c6a426e4966a59511632a4fc891bdffef577d255a040eb8',
    'boss5':'b3a3d9abbad73aee2719447bbb6f9fc7c712bc01d4d82290688022545891c600',
    'insufficient':'d6a81ad0f3c7dd0503b2d84b0a4e6ea98b65e085016de244088ecc4a3e744c55',
}


def read_sample(sample, page, boss=5, *, simulation=False, health_only=False):
    plan = dict(sampled_plan(),boss=boss)
    profile = load_calibration(plan)
    labels = deepcopy(profile['pages']['lobby' if page=='lobby' else 'boss_detail']['labels'])
    fields = (dict(remaining_count=[540,520,45,40],carry_count=[740,520,45,40],cp=[740,570,92,42])
        if page=='lobby' else dict(remaining_count=[1138,485,55,37],cp=[1105,532,90,40],
            health=[690,402,240,45],**profile['pages']['boss_detail']['fields']))
    if health_only:
        fields={key:value for key,value in fields.items() if key in ('health','boss','phase')}
    rejects = [dict(roi=[460,90,350,60],expected='^前往主线关卡$'),
               dict(roi=[760,525,260,55],expected='^设定为模拟战[。.]?$')]
    # Deliberately partial and local to this saved-frame check; no production grant.
    profile['daily'] = dict(cp_semantics='daily_earned',pages={page:dict(labels=labels,rejects=rejects,fields=fields)})
    nodes = {}
    for kind, specs in (('labels',labels),('rejects',rejects)):
        for index, spec in enumerate(specs):
            nodes[f'CBDaily_{page}_{kind}_{index}'] = _node(spec)
    for field, roi in fields.items():
        pattern = PATTERNS.get(field)
        if field=='boss':
            import re
            pattern='^'+re.escape(profile['boss_targets'][str(boss)]['name'])+'$'
        nodes[f'CBDaily_{page}_{field}'] = _node(dict(roi=roi),expected=pattern)
    entry='CBDailyPage_'+page
    nodes[entry] = dict(recognition='Custom',custom_recognition='ClanBattleDailyPage',custom_recognition_param=dict(page=page),
        action='DoNothing',next=[],timeout=1000,pre_delay=0,post_delay=0)
    directory=native.ROOT/'assets/resource/clan-battle-calibrations/cn-bilibili-202610141448/samples'
    path=(next(row['path'] for row in profile['samples'] if row['page']=='target')
          if simulation else directory/f'formal-readback-{sample}.png')
    if not simulation:
        assert hashlib.sha256(path.read_bytes()).hexdigest()==SAMPLES[sample]
    resource=native.load_resource()
    callback=ClanBattleDailyPage(profile,plan)
    errors=[]
    original_read=callback.read
    def diagnosed_read(*args):
        try:
            return original_read(*args)
        except Exception as exc:
            name=str(exc).split(':')[-1]
            seen=args[0].run_recognition(name,args[1]) if name.startswith('CBDaily_') else None
            errors.append(dict(error=str(exc),raw=getattr(seen,'raw_detail',None)))
            raise
    callback.read=diagnosed_read
    assert resource.register_custom_recognition('ClanBattleDailyPage',callback)
    assert resource.override_pipeline(game_session_pipeline(int(plan['account_id'])))
    assert resource.override_pipeline(nodes)
    controller=SavedImages([[native.read_image(path)]])
    tasker=native.bind(resource,controller)
    tasker._resource_holder=resource
    try:
        receipt=native.run(tasker,entry)
        assert not controller.clicks and not controller.swipes
        receipt['read_errors']=errors
        return receipt
    finally:
        stopped=native.stop_tasker(tasker)
        assert stopped['idle'] and stopped['stop_completed']


def test_actual_lobby_reads_cp_and_both_zero_counters_without_fabricating_units():
    receipt=read_sample('lobby','lobby')
    assert receipt['native_ok'],receipt['read_errors']
    reading=_detail(receipt)
    assert reading['remaining_count']==reading['carry_count']==reading['cp']==0
    assert reading['cp_semantics']=='daily_earned'
    assert not {'used_unit','carry_stock_indices','instance_id','round'} & reading.keys()


def test_actual_living_formal_target_reads_boss_and_current_counters():
    receipt=read_sample('boss3','target',3)
    assert receipt['native_ok'],receipt['read_errors']
    reading=_detail(receipt)
    assert reading['formal'] is True and reading['boss']==3 and reading['phase']=='B'
    assert reading['cp']==reading['remaining_count']==0 and reading['health']==50000000
    assert reading['alive'] is True
    assert not {'instance_id','round','used_unit'} & reading.keys()


def test_actual_defeated_boss_health_does_not_fabricate_an_unread_counter():
    receipt=read_sample('boss5','target',health_only=True)
    assert receipt['native_ok'],receipt['read_errors']
    reading=_detail(receipt)
    assert reading['formal'] is True and reading['boss']==5 and reading['health']==0 and reading['alive'] is False
    assert not {'remaining_count','cp','round','instance_id'} & reading.keys()


def test_uncertain_defeated_counter_stops_the_complete_formal_observation():
    receipt=read_sample('boss5','target')
    assert not receipt['native_ok']
    assert receipt['read_errors'] and all(error['error'].endswith('CBDaily_target_remaining_count') for error in receipt['read_errors'])


def test_insufficient_cp_popup_cannot_be_read_as_its_background_formal_target():
    assert not read_sample('insufficient','target',3)['native_ok']


def test_simulation_target_cannot_supply_formal_counter_observation():
    assert not read_sample('boss5','target',simulation=True)['native_ok']
