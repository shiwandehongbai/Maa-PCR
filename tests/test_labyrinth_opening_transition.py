"""Opening transitions must outlast a map glimpse without inventing an invitation."""
from types import SimpleNamespace as NS

import pytest

from pcr_maa import native
from pcr_maa.labyrinth import game_port
from pcr_maa.labyrinth.batch import BatchStopped
from pcr_maa.labyrinth.timing import Timings


@pytest.fixture
def transition(monkeypatch):
    state = NS(now=0., pages=[], reads=[], inputs=[], observations=[], stopped=False)
    monkeypatch.setattr(game_port, 'time', NS(monotonic=lambda: state.now), raising=False)
    monkeypatch.setattr('pcr_maa.native.active_now',game_port.time.monotonic)
    resource = NS(override_pipeline=lambda nodes: True)
    owner = NS(resource=resource, stopping=False)
    port = game_port.MaaGamePort(owner, lambda **fields: fields)

    def checkpoint():
        if state.stopped:
            raise BatchStopped('stopped')

    def transaction(operation, fields, send):
        state.inputs.append(operation)
        return send(str(len(state.inputs)))

    def observation(value, **fields):
        state.observations.append(value['page'])
        return value

    batch = NS(checkpoint=checkpoint, stopped=lambda: state.stopped, timings=Timings(),
        transaction=transaction, observation=observation)

    def run(tasker, entry, **kwargs):
        state.reads.append((entry, native.RUN_DEADLINE.get()))
        if entry.endswith('PollSpacing'):
            state.now += .5
            root = dict(name=entry, completed=True, action=dict(success=True))
        elif entry in ('LabyClientResumeHomeDepartOnce', 'LabyDepartureRewardCloseOnce'):
            root = dict(name=entry, completed=True, action=dict(success=True))
        else:
            page, seconds = state.pages.pop(0) if len(state.pages) > 1 else state.pages[0]
            state.now += seconds
            names = game_port.game_port_pipeline()[entry]['any_of']
            names = list(dict.fromkeys([*names, 'LabyMapObserved']))
            names = names[:names.index(page) + 1]
            root = dict(name=entry, completed=True, recognition=[dict(name=name, algorithm='And',
                box=[0, 0, 1, 1] if name == page else None) for name in names])
        return dict(native_ok=True, tasker_inited=True, task_id=len(state.reads), nodes=[root])

    monkeypatch.setattr(native, 'run', run)
    state.port, state.batch = port, batch
    token = native.RUN_DEADLINE.set(float('inf'))
    yield state
    native.RUN_DEADLINE.reset(token)


@pytest.mark.parametrize('entry', ['LabyGamePortOpeningObserved', 'LabyGamePortRosterObserved'])
def test_map_glimpse_waits_for_actual_empty_selection(transition, entry):
    value = transition
    value.pages = [('LabyMapObserved', 3.), ('LabyMapObserved', 3.), ('LabyOpeningEmptyObserved', 1.)]
    names = game_port.game_port_pipeline()[entry]['any_of']
    page, receipt = value.port._wait(value.batch, entry, names)
    assert page == 'LabyOpeningEmptyObserved' and value.now > 5
    assert receipt['native_ok'] and value.inputs == []
    assert native.RUN_DEADLINE.get() == float('inf')


def test_departure_reward_and_following_map_do_not_repeat_inputs(transition):
    value = transition
    value.pages = [('LabyMapObserved', 2.), ('LabyDepartureRewardObserved', 1.),
        ('LabyMapObserved', 2.), ('LabyOpeningEmptyObserved', 1.)]
    assert value.port._initial_roster(value.batch, NS(enter_id=1, guild_id=3)) is None
    assert value.inputs == ['opening_depart', 'opening_departure_reward_close']
    assert value.observations == ['LabyDepartureRewardObserved', 'LabyOpeningEmptyObserved']


@pytest.mark.parametrize('entry', ['LabyGamePortOpeningObserved', 'LabyGamePortRosterObserved'])
def test_stable_map_never_proves_selection_complete(transition, entry):
    value = transition
    value.pages = [('LabyMapObserved', 2.)]
    names = game_port.game_port_pipeline()[entry]['any_of']
    with pytest.raises(BatchStopped, match='^game_session_readback_unknown$'):
        value.port._wait(value.batch, entry, names)
    assert 5 < value.now <= 22 and value.inputs == []
    assert native.RUN_DEADLINE.get() == float('inf')


@pytest.mark.parametrize('parent', [.5, 4.])
def test_opening_wait_respects_earlier_parent_deadline(transition, parent):
    value = transition
    value.pages = [('LabyMapObserved', 1.)]
    token = native.RUN_DEADLINE.set(parent)
    try:
        names = game_port.game_port_pipeline()['LabyGamePortOpeningObserved']['any_of']
        with pytest.raises(BatchStopped, match='^game_session_readback_unknown$'):
            value.port._wait(value.batch, 'LabyGamePortOpeningObserved', names)
        assert value.now < parent + 2 and value.inputs == []
        assert all(deadline <= parent for _, deadline in value.reads)
        assert native.RUN_DEADLINE.get() == parent
    finally:
        native.RUN_DEADLINE.reset(token)


def test_stop_before_opening_read_remains_immediate(transition):
    value = transition
    value.stopped = True
    with pytest.raises(BatchStopped, match='^stopped$'):
        value.port._wait(value.batch, 'LabyGamePortOpeningObserved', [])
    assert value.reads == [] and value.inputs == []


@pytest.mark.parametrize('next_node,page', [('LabyOpeningEmptyObserved', 'opening_empty'), ('LabyMapObserved', 'map')])
def test_granted_character_close_waits_for_real_selection(transition, monkeypatch, next_node, page):
    from pcr_maa.labyrinth import opening, scene
    from test_labyrinth_joined_confirmation import callback, receipt

    value = transition
    actual = receipt(callback(monkeypatch, roles=('1068',), names=('拉比林斯达',)).detail)
    port = opening.MaaOpening(value.port.tasker, lambda **fields: fields)
    port.tasker.resource.get_node_data = lambda name: {}
    port.observe_joined = lambda *args: dict(layout='joined1', roles_joined=['1068'], native=actual)
    port._run = lambda *args, **kwargs: actual
    port._choice = lambda batch, entry, names: (names[-1], actual)
    waits = []
    monkeypatch.setattr(scene, 'MaaSceneRuntime', lambda *args, **kwargs:
        NS(wait_for=lambda *args, **kwargs: waits.append((args, kwargs))))
    result = port.consume_granted(value.batch, 1, guild_id=5, next_node=next_node, page=page)
    assert result['roles_joined'] == ['1068']
    assert value.inputs == ['opening_granted_close']
    assert len(waits) == (1 if page == 'opening_empty' else 0)
    if waits:
        assert waits[0][0][1] == {'free_role_reward'}
        assert waits[0][1]['source'] == {'map', 'joined'}
        assert waits[0][1]['timeout'] == scene.OBSERVATION_TIMEOUT_SECONDS
