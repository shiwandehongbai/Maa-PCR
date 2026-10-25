import io
import asyncio
import json
import threading
import queue
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pcr_maa import native
from pcr_maa.labyrinth.batch import Batch, BatchStopped, Binding
from pcr_maa.labyrinth.config import Settings, Strategy
from pcr_maa.labyrinth.owned_runtime import StopSignal
from pcr_maa.preset_execution import Journal


def settings():
    return Settings.parse(dict(version=1,goals=[dict(guild_id=1,clears=1)]))


def test_pause_freezes_business_clock_and_resume_keeps_same_owner(monkeypatch):
    wall=[100.0]
    monkeypatch.setattr('pcr_maa.labyrinth.owned_runtime.time.monotonic',lambda:wall[0])
    paused=threading.Event()
    resumed=threading.Event()
    messages=[]
    signal=StopSignal(io.StringIO(),lambda value:(messages.append(value),paused.set()))
    identity=[]
    def owner():
        signal.owner_thread=threading.get_ident()
        token=native.RUN_CONTROL.set(signal)
        try:
            identity.append(id(signal))
            signal.set_paused(True)
            signal.checkpoint()
            identity.append(id(signal))
            assert native.active_now()==100
            wall[0]+=5
            assert native.active_now()==105
        finally:
            native.RUN_CONTROL.reset(token)
            resumed.set()
    task=threading.Thread(target=owner)
    task.start()
    assert paused.wait(2)
    wall[0]=165
    assert signal.now()==100
    wall[0]=160
    signal.set_paused(False)
    assert resumed.wait(2)
    task.join(2)
    assert not task.is_alive()
    assert identity[0]==identity[1]
    assert signal.now()==105
    assert messages==[dict(kind='control',state='paused'),dict(kind='control',state='running')]


def test_pause_ack_waits_for_settlement_and_stop_unblocks():
    messages=[]
    signal=StopSignal(io.StringIO(),messages.append)
    with signal.defer():
        signal.set_paused(True)
        signal.checkpoint()
        assert messages==[]
        assert signal.transaction_depth==1
    signal.request()
    signal.checkpoint()
    assert signal()
    assert messages==[]


def test_stop_wakes_a_confirmed_pause():
    paused=threading.Event()
    signal=StopSignal(io.StringIO(),lambda value:paused.set())
    def owner():
        signal.owner_thread=threading.get_ident()
        signal.set_paused(True)
        signal.checkpoint()
    task=threading.Thread(target=owner)
    task.start()
    assert paused.wait(2)
    signal.request()
    task.join(2)
    assert not task.is_alive() and signal()


@pytest.mark.parametrize('tail',[{'kind':'start'},{'kind':'pause','settings':{}},{'kind':True}])
def test_control_channel_cannot_change_account_or_configuration(tail):
    signal=StopSignal(io.StringIO(json.dumps(tail)+'\n'))
    signal._read()
    assert signal()


def test_controls_are_multiple_commands_but_eof_revokes_owner():
    signal=StopSignal(io.StringIO('{"kind":"pause"}\n{"kind":"resume"}\n'))
    signal._read()
    assert not signal.pause_requested and signal()


@pytest.mark.parametrize('interval',[300,500])
@pytest.mark.parametrize('normalized',[False,True])
def test_response_interval_paces_observation_not_input(monkeypatch,interval,normalized):
    wall=[100.0]
    monkeypatch.setattr('pcr_maa.labyrinth.owned_runtime.time.monotonic',lambda:wall[0])
    signal=StopSignal(io.StringIO(),response_interval_ms=interval)
    monkeypatch.setattr(signal.event,'wait',lambda seconds:wall.__setitem__(0,wall[0]+seconds))
    nodes={'read':dict(action='DoNothing',next=[],rate_limit=250),
        'click':dict(action='Click',next=[],rate_limit=250)}
    if normalized:
        for node in nodes.values():node['action']=dict(type=node['action'],param={})
    def override(value):
        for key,props in value.items():nodes[key].update(props)
        return True
    tasker=SimpleNamespace(resource=SimpleNamespace(get_node_data=lambda key:nodes[key],override_pipeline=override))
    signal.before_dispatch(tasker,'read')
    signal.before_dispatch(tasker,'read')
    assert wall[0]==pytest.approx(100+interval/1000)
    signal.before_dispatch(tasker,'click')
    assert wall[0]==pytest.approx(100+interval/1000)
    assert nodes['click']['rate_limit']==250
    assert nodes['read']['rate_limit']==interval


def test_batch_checkpoint_participates_in_pause():
    called=[]
    signal=StopSignal(io.StringIO())
    signal.checkpoint=lambda:called.append('checkpoint')
    batch=Batch(settings(),object(),object(),Binding(1,'test'),stopped=signal)
    batch.checkpoint()
    assert called==['checkpoint']


def test_pause_keeps_settled_input_and_budget_without_resending(tmp_path):
    journal=Journal(tmp_path/'ledger')
    paused=threading.Event()
    messages=[]
    errors=[]
    signal=StopSignal(io.StringIO())
    calls=[]
    def emit(value):
        assert journal.last_event()['state']=='settled'
        messages.append(value)
        paused.set()
    signal.emit=emit
    def owner():
        signal.owner_thread=threading.get_ident()
        token=native.RUN_CONTROL.set(signal)
        try:
            batch=Batch(settings(),object(),journal,Binding(1,'test'),stopped=signal,clock=signal.now)
            start=native.active_now()
            def send(oid):
                calls.append(oid)
                signal.set_paused(True)
                batch.checkpoint()
                assert messages==[]
                return dict(verified=True,binding=batch.binding.payload(),frame=1,
                    operation_id=oid,enter_id=1)
            with journal.locked():
                result=batch.transaction('enter',dict(enter_id=1),send)
                assert journal.last_event()['result']==result
            batch.checkpoint()
            assert native.active_now()-start < 1
        except BaseException as error:errors.append(error)
        finally:native.RUN_CONTROL.reset(token)
    task=threading.Thread(target=owner)
    task.start()
    assert paused.wait(3)
    assert len(calls)==1
    signal.set_paused(False)
    task.join(3)
    assert not task.is_alive()
    assert errors==[] and len(calls)==1
    assert [value['state'] for value in messages]==['paused','running']


@pytest.mark.parametrize('value',[
    dict(response_interval_ms=250),dict(response_interval_ms=True),
    dict(special_equipment_mode='yes'),dict(special_equipment_mode='auto_equip',special_equipment_battles=[]),
    dict(special_equipment_battles=['EX','EX']),dict(special_equipment_battles=['SHOP']),
    dict(auto_database_update=1),
])
def test_new_strategy_fields_fail_closed(value):
    with pytest.raises(ValueError):Strategy.parse(value)


def test_strategy_snapshot_round_trip_and_default():
    strategy=Strategy.parse(dict(response_interval_ms=500,special_equipment_mode='auto_equip',
        special_equipment_battles=['EX','BOSS'],auto_database_update=False))
    assert Strategy.parse(strategy.payload())==strategy
    assert Strategy().special_equipment_mode=='off'
    assert Strategy().response_interval_ms==300


def test_real_owner_pipe_pauses_and_resumes_same_process(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=tmp_path/'pipe-owner.py'
    script.write_text('''import json,sys,time
sys.path.insert(0,sys.argv[1])
from types import SimpleNamespace
from pcr_maa.labyrinth.owned_runtime import run_owned
from pcr_maa.labyrinth.config import Strategy
output=sys.stdout
def emit(value):
    output.write(json.dumps(value)+'\\n');output.flush()
class Request:
    settings=SimpleNamespace(strategy=Strategy())
    def clear(self):pass
class Runtime:
    def __init__(self,request,stop,emit):self.stop,self.emit=stop,emit
    def run(self):
        count=0
        while True:
            self.stop.checkpoint()
            if self.stop():return dict(kind='result',count=count)
            count+=1
            self.emit(dict(kind='frame',count=count))
            time.sleep(.02)
emit(run_owned(Request(),sys.stdin,emit,runtime_factory=Runtime))
''',encoding='utf-8')
    child=subprocess.Popen([sys.executable,'-X','utf8','-B','-u',str(script),str(root/'src')],
        cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    lines=queue.Queue()
    def read():
        for line in child.stdout:lines.put(json.loads(line))
    reader=threading.Thread(target=read,daemon=True);reader.start()
    def until(kind,state=None):
        for _ in range(300):
            value=lines.get(timeout=5)
            if value['kind']==kind and (state is None or value.get('state')==state):return value
        pytest.fail('owner control acknowledgement missing')
    def send(kind):
        child.stdin.write(json.dumps(dict(kind=kind))+'\n');child.stdin.flush()
    try:
        before=until('frame')['count']
        pid=child.pid
        send('pause');until('control','paused')
        with pytest.raises(queue.Empty):lines.get(timeout=.15)
        assert child.poll() is None and child.pid==pid
        send('resume');until('control','running')
        after=until('frame')['count']
        assert after>before and child.pid==pid
        send('stop');result=until('result')
        child.wait(timeout=5)
        assert child.returncode==0 and result['count']>=after
        assert child.stderr.read()==''
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
        reader.join(timeout=5)
        for stream in (child.stdin,child.stdout,child.stderr):stream.close()


def test_pause_does_not_suspend_an_active_protocol_async_timeout():
    paused=threading.Event()
    messages=[]
    signal=StopSignal(io.StringIO(),lambda value:(messages.append(value),paused.set()))
    def resume():
        if paused.wait(2):
            time.sleep(.15)
            signal.set_paused(False)
    helper=threading.Thread(target=resume)
    helper.start()
    async def protocol():
        async with asyncio.timeout(.1):
            signal.set_paused(True)
            signal.checkpoint()
            assert messages==[], 'pause must wait until the protocol request finishes'
            await asyncio.sleep(.01)
    try:
        asyncio.run(protocol())
        assert signal.pause_requested and messages==[]
        signal.checkpoint()
        assert [message['state'] for message in messages]==['paused','running']
    finally:
        signal.set_paused(False)
        paused.set()
        helper.join(3)
        assert not helper.is_alive()
