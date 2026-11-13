"""Regression for the game update that expanded 黎明界 departure guilds 5 -> 10.

Guild ids remain the selection ordinal 1..10 and the protocol ENTER request is
assumed to take that same ordinal; only the old 1..5 clamp is proven stale.
"""
import asyncio
from types import SimpleNamespace

import pytest

from pcr_maa.labyrinth.batch import BatchStopped, Binding
from pcr_maa.labyrinth.config import Settings, Strategy
from pcr_maa.labyrinth.game_port import MaaGamePort
from pcr_maa.labyrinth.opening import MaaOpening
from pcr_maa.labyrinth.protocol import Endpoint, LabyrinthClient, ProtocolFailure
from pcr_maa.labyrinth.restart import _valid_intent, pending_intent
from pcr_maa.labyrinth.routes import Node
from pcr_maa.labyrinth.snapshots import OpeningSnapshot
from pcr_maa.labyrinth.timing import Timings
from pcr_maa.preset_execution import Journal


def goals(*ids):
    return [dict(guild_id=guild, clears=1) for guild in ids]


def roster():
    return [['1049'], ['1091'], ['1171']]


def opening_map():
    nodes = []
    for area in (1, 2, 3):
        kinds = (1, 6, 8) if area == 3 else (1, 6, 6)
        for column, kind in enumerate(kinds, 1):
            uid = area * 100 + column
            quest = 7703 * 100000 + 4 * 100 + 1 if kind == 8 else None
            nodes.append(Node(uid, area, column, 1, kind,
                              (uid + 1,) if column < len(kinds) else (), quest))
    return [node.payload() for node in nodes]


def fake_client():
    client = LabyrinthClient.__new__(LabyrinthClient)
    client.logged_in = True
    client._phase = 'protocol'
    client.lock = asyncio.Lock()
    client._active_task = None
    client.timings = Timings()

    def close():
        client._phase = 'closed'
        client.logged_in = False
    client.close = close
    calls = []

    async def locked(endpoint, fields):
        calls.append((endpoint, fields))
        return dict(enter_id=169, map_list=opening_map())
    client._request_locked = locked
    return client, calls


def test_settings_accept_all_ten_guild_goals_and_payload_round_trip():
    settings = Settings.parse(dict(version=1, goals=goals(*range(1, 11))))
    assert [goal.guild_id for goal in settings.goals] == list(range(1, 11))
    assert Settings.parse(settings.payload()) == settings


@pytest.mark.parametrize('goal', [{'guild_id': 0, 'clears': 1}, {'guild_id': 11, 'clears': 1}])
def test_settings_still_reject_goals_outside_one_to_ten(goal):
    with pytest.raises(ValueError, match='^invalid_goal$'):
        Settings.parse(dict(version=1, goals=[goal]))


def test_settings_reject_more_than_ten_goals():
    with pytest.raises(ValueError, match='^invalid_goals$'):
        Settings.parse(dict(version=1, goals=goals(*range(1, 12))))


def test_strategy_accepts_tenth_guild_rosters_and_units():
    value = dict(opening_rosters={'10': roster()}, guild_units={'10': ['1049', '1091', '1171']})
    strategy = Strategy.parse(value)
    assert strategy.opening_rosters['10'] == (('1049',), ('1091',), ('1171',))
    assert strategy.guild_units['10'] == ('1049', '1091', '1171')
    settings = Settings.parse(dict(version=1, goals=goals(10), strategy=value))
    payload = settings.strategy.payload()
    assert payload['opening_rosters'] == {'10': [['1049'], ['1091'], ['1171']]}
    assert payload['guild_units'] == {'10': ['1049', '1091', '1171']}
    assert Strategy.parse(payload).guild_units['10'] == ('1049', '1091', '1171')


@pytest.mark.parametrize('field,error', [('opening_rosters', 'invalid_opening_rosters'),
                                        ('guild_units', 'invalid_guild_units')])
def test_strategy_still_rejects_keys_outside_one_to_ten(field, error):
    value = {'opening_rosters': {'11': roster()}, 'guild_units': {'11': ['1049']}}[field]
    with pytest.raises(ValueError, match='^' + error + '$'):
        Strategy.parse({field: value})


@pytest.mark.parametrize('guild,difficulty', [(10, 1), (10, 5), (6, 2), (1, 5)])
def test_protocol_enter_accepts_tenth_guild(guild, difficulty):
    client, calls = fake_client()
    value = asyncio.run(client.enter(guild, difficulty))
    assert value['enter_id'] == 169
    assert calls == [(Endpoint.ENTER, dict(guild_id=guild, difficulty=difficulty))]


@pytest.mark.parametrize('guild,difficulty', [(11, 1), (0, 1), (True, 1), (10, 6), (10, True)])
def test_protocol_enter_still_rejects_values_outside_the_ranges(guild, difficulty):
    client, calls = fake_client()
    with pytest.raises(ProtocolFailure, match='^request_rejected$'):
        asyncio.run(client.enter(guild, difficulty))
    assert calls == []


def test_opening_snapshot_accepts_tenth_guild():
    resume = dict(guild_id=10, block_id=0, map_list=opening_map())
    snapshot = OpeningSnapshot.read(123, 169, dict(enter_id=169, guild_id=10, difficulty=1), resume)
    assert snapshot.guild_id == 10


@pytest.mark.parametrize('guild,difficulty', [(11, 1), (10, 6)])
def test_opening_snapshot_still_rejects_out_of_range_identity(guild, difficulty):
    resume = dict(guild_id=guild, block_id=0, map_list=opening_map())
    with pytest.raises(ValueError, match='^opening_identity_unknown$'):
        OpeningSnapshot.read(123, 169, dict(enter_id=169, guild_id=guild, difficulty=difficulty), resume)


def test_game_port_opening_scope_accepts_tenth_guild():
    port = MaaGamePort(None, lambda **fields: dict(**fields))
    batch = SimpleNamespace(settings=SimpleNamespace(difficulty=1),
        binding=Binding(123, 'synthetic-device'), checkpoint=lambda: None)
    with pytest.raises(BatchStopped, match='^game_port_creation_unknown$'):
        port.restore_opening(OpeningSnapshot(123, 169, 10, 1, ()), batch, creation={})
    with pytest.raises(BatchStopped, match='^game_port_opening_scope_unqualified$'):
        port.restore_opening(OpeningSnapshot(123, 169, 11, 1, ()), batch, creation={})


INTENT = dict(state='in_flight', operation='enter', operation_id='a' * 32,
              binding=dict(account_id=123, device_id='synthetic-device'))


@pytest.mark.parametrize('guild,difficulty', [(10, 5), (10, 1), (1, 5)])
def test_restart_enter_intent_accepts_tenth_guild(guild, difficulty):
    assert _valid_intent(dict(INTENT, guild_id=guild, difficulty=difficulty)) is True


@pytest.mark.parametrize('changes', [dict(guild_id=11, difficulty=1), dict(guild_id=10, difficulty=6),
    dict(guild_id=10, difficulty=True), dict(guild_id=10, difficulty=1, enter_id=169)])
def test_restart_enter_intent_still_rejects_invalid_scope(changes):
    assert _valid_intent(dict(INTENT, **changes)) is False


@pytest.mark.parametrize('guild,valid', [(10, True), (11, False)])
def test_restart_pending_enter_intent_scope(tmp_path, guild, valid):
    binding = Binding(123, 'synthetic-device')
    journal = Journal(tmp_path)
    row = dict(INTENT, binding=binding.payload(), guild_id=guild, difficulty=5)
    with journal.locked():
        journal.append(row)
    if valid:
        assert pending_intent(journal, binding) == row
    else:
        with pytest.raises(BatchStopped, match='^blocked_in_flight$'):
            pending_intent(journal, binding)


def test_opening_invite_requires_explicit_roster_for_new_guilds():
    opening = MaaOpening(None, lambda **fields: dict(**fields))
    for guild in (6, 10):
        with pytest.raises(BatchStopped, match='^opening_roster_required$'):
            opening.invite(None, 169, guild_id=guild)


def test_opening_invite_keeps_upstream_defaults_only_for_first_five_guilds():
    opening = MaaOpening(None, lambda **fields: dict(**fields))
    opening.used.add(169)
    for guild in range(1, 6):
        # Reaching the consumed-enter guard proves the upstream default roster
        # was resolved; the guard itself supplies no new evidence.
        with pytest.raises(BatchStopped, match='^opening_already_attempted$'):
            opening.invite(None, 169, guild_id=guild)
    # New guilds must not be silently mapped onto an old default roster.
    with pytest.raises(BatchStopped, match='^opening_roster_required$'):
        opening.invite(None, 169, guild_id=6)


def test_opening_invite_accepts_explicit_roster_for_new_guild():
    opening = MaaOpening(None, lambda **fields: dict(**fields))
    opening.used.add(169)
    with pytest.raises(BatchStopped, match='^opening_already_attempted$'):
        opening.invite(None, 169, guild_id=10, slots=roster())


def test_opening_invite_still_rejects_guild_eleven():
    opening = MaaOpening(None, lambda **fields: dict(**fields))
    with pytest.raises(BatchStopped, match='^opening_identity_required$'):
        opening.invite(None, 169, guild_id=11)
