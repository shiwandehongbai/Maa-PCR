"""Frozen upstream isolated-front rules, exercised through the shared planner."""
from dataclasses import replace

import pytest

from pcr_maa.labyrinth.encounters import BY_ID, guide_core
from pcr_maa.labyrinth.roles import Context, Optimizer, load_optimizer
from test_labyrinth_roles import profile


GHOST = BY_ID['ghost_lord']


def optimizer(roles, *, bound=None):
    config = dict(load_optimizer().config)
    if bound is not None:
        config['exhaustiveRosterLimit'] = bound
    return Optimizer({p.id: p for p in roles}, config)


def context(**kwargs):
    return Context(defense=4, encounter=GHOST, **kwargs)


def isolated_roster(extra=0):
    return [profile('tank', 100, rating=1, tank=True),
            profile('melee', 200, rating=100),
            *[profile('rear' + str(i), 501 + i * 25,
                      'MAGIC' if i == 0 else 'PHYSICAL', rating=10 + i)
              for i in range(4 + extra)]]


def test_ghost_fills_only_unknown_enemy_type_and_leaves_explicit_knowledge():
    assert GHOST.enemy == 'MAGIC' and GHOST.isolation_radius == 300
    assert context().effective_enemy == 'MAGIC'
    for enemy in ('PHYSICAL', 'MAGIC', 'MIXED'):
        assert context(enemy=enemy).effective_enemy == enemy
    assert Context().effective_enemy == 'UNKNOWN'


def test_magic_default_reaches_vanguard_panel_evasion_and_attack_reduction():
    tank = profile('tank', 100, tank=True)
    tank.data['vanguardProfile'] = dict(physicalDurability=10, magicDurability=80,
        physicalEvasion=0, magicEvasion=35)
    tank.data['functions'].update(physicalAttackReduction=0, magicAttackReduction=95)
    roles = [tank, *[profile('rear' + str(i), 501 + i * 10) for i in range(4)]]
    a = optimizer(roles); ids = tuple(a.profiles)
    assert tank.vanguard(context()) == tank.vanguard(context(enemy='MAGIC'))
    assert tank.vanguard(context()) > tank.vanguard(context(enemy='PHYSICAL'))
    assert a.score(ids, context()) == a.score(ids, context(enemy='MAGIC'))
    assert a.score(ids, context()).system_score > a.score(ids, context(enemy='PHYSICAL')).system_score


def test_risk_counts_radius_boundary_unknown_position_and_extra_tanks():
    from pcr_maa.labyrinth.encounters import formation_risk
    front = profile('front', 100, tank=True)
    assert formation_risk([front, profile('edge', 400)], context()) == 1
    assert formation_risk([front, profile('rear', 401)], context()) == 0
    assert formation_risk([front, profile('unknown', None)], context()) == 1
    assert formation_risk([front, profile('second_tank', 900, tank=True)], context()) == 1
    assert formation_risk([profile('a', None), profile('b', None)], context()) == 1
    assert formation_risk([], context()) == 0
    assert formation_risk([front, profile('edge', 100)], Context()) == 0


@pytest.mark.parametrize('prefer_single', [False, True])
@pytest.mark.parametrize('system_only', [False, True])
def test_initial_search_prefers_four_safe_rear_members_over_rating_and_cohesion(prefer_single, system_only):
    roles = isolated_roster(); a = optimizer(roles)
    result = a.best(tuple(a.profiles), context(prefer_single=prefer_single),
                    battle=True, system_only=system_only)
    assert set(result.members) == {'tank', 'rear0', 'rear1', 'rear2', 'rear3'}


def test_bounded_pool_keeps_low_rated_safe_rear_cohort():
    roles = isolated_roster() + [profile('near' + str(i), 210 + i, rating=99) for i in range(18)]
    a = optimizer(roles, bound=5)
    result = a.best(tuple(a.profiles), context(), battle=True)
    assert set(result.members) == {'tank', 'rear0', 'rear1', 'rear2', 'rear3'}


def test_effective_core_filters_nearby_member_and_keeps_safe_member():
    a = optimizer(isolated_roster())
    c = context(effective_ids=('melee', 'rear0'))
    assert guide_core(a, tuple(a.profiles), c) == ('rear0',)
    result = a.best(tuple(a.profiles), c, battle=True)
    assert 'rear0' in result.members and 'melee' not in result.members


def test_effective_extra_tank_cannot_override_safer_ordinary_formation():
    roles = [p for p in isolated_roster() if p.id != 'melee']
    roles.append(profile('extra_tank', 400, rating=100, tank=True))
    a = optimizer(roles)
    c = context(effective_ids=('extra_tank',))
    assert guide_core(a, tuple(a.profiles), c) == ('extra_tank',)
    result = a.best(tuple(a.profiles), c, battle=True)
    assert set(result.members) == {'tank', 'rear0', 'rear1', 'rear2', 'rear3'}
    forced = a.best(tuple(a.profiles), c, battle=True, required=('extra_tank',))
    assert 'extra_tank' in forced.members


def test_retry_keeps_isolation_and_excludes_reordered_failed_team():
    a = optimizer(isolated_roster(extra=1)); ids = tuple(a.profiles); c = context(prefer_single=True)
    first = a.best(ids, c, battle=True)
    result = a.retry_best(ids, c, failed_teams=(tuple(reversed(first.members)),))
    assert set(result.members) != set(first.members)
    assert 'tank' in result.members and 'melee' not in result.members
    assert len(result.members) == 5 and set(result.members) <= set(ids)
    assert a.retry_best(first.members, c, failed_teams=(first.members,)) is None


@pytest.mark.parametrize('healer_position,expect_healer', [(200, False), (900, True)])
def test_second_failure_healer_swap_cannot_raise_best_retry_risk(healer_position, expect_healer):
    roles = isolated_roster(extra=1)
    roles = [p for p in roles if p.id != 'melee']
    healer = profile('healer', healer_position, rating=80)
    healer.data['roleClass'] = '治疗者'; healer.data['functions']['healing'] = 90
    a = optimizer([*roles, healer]); ids = tuple(a.profiles)
    failed = ('tank', 'rear0', 'rear1', 'rear2', 'rear3')
    result = a.retry_best(ids, context(), failed_teams=(failed,),
                          last_failed_team=failed, retry_number=2)
    assert ('healer' in result.members) is expect_healer
    assert 'tank' in result.members and set(result.members) != set(failed)


def test_non_ghost_keeps_high_rating_and_explicit_required_member():
    a = optimizer(isolated_roster()); ids = tuple(a.profiles)
    plain = a.best(ids, Context(defense=4), battle=True)
    assert 'melee' in plain.members
    required = a.best(ids, context(), battle=True, required=('melee',))
    assert 'melee' in required.members
    assert a.best(('tank', 'rear0', 'missing'), context(), battle=True) is None


def test_unknown_positions_are_never_invented_for_battle():
    roles = isolated_roster(); roles[2].position = None
    a = optimizer(roles)
    result = a.best(tuple(a.profiles), context(), battle=True)
    assert result is not None and 'rear0' not in result.members
    roles = [p for p in roles if p.id != 'melee']
    assert optimizer(roles).best(tuple(p.id for p in roles), context(), battle=True).members == (
        'tank', 'rear1', 'rear2', 'rear3')
