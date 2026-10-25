"""Scalar diagnostic collection and real event guards; no device or credentials."""
import json
import time
from types import SimpleNamespace as NS

import numpy as np
import pytest

from pcr_maa.labyrinth import diagnostics as d
from pcr_maa.labyrinth.event_observation import EventRecognition,catalogue,pixel_rect
from pcr_maa.labyrinth.timing import NativeTimings,Timings


def contents(path):
    return '\n'.join(file.read_text(encoding='utf-8') for file in path.iterdir() if file.is_file())


def test_rolling_samples_report_one_core_cpu_and_keep_only_allowed_memory(tmp_path,monkeypatch):
    moment=[100.,3.]
    monkeypatch.setattr(d,'process_start_elapsed',lambda:50.)
    diag=d.RuntimeDiagnostics(tmp_path,max_samples=2,clock=lambda:moment[0],cpu_clock=lambda:moment[1],
        wall_clock=lambda:1000+moment[0],memory=lambda:dict(processWorkingSetBytes=123,hostTotalMemoryBytes=456,password='synthetic-secret'))
    assert diag.sample()['processCpuPercentOfOneCore'] is None
    moment[:]=[102.,4.]
    assert diag.sample()['processCpuPercentOfOneCore']==50
    moment[:]=[104.,4.5]
    result=diag.sample()
    assert result['processCpuPercentOfOneCore']==25
    assert result['processAgeMillis']==54000 and result['processWorkingSetBytes']==123
    rows=[json.loads(line) for line in (tmp_path/'runtime-history.ndjson').read_text(encoding='utf-8').splitlines()]
    assert len(rows)==2 and rows[0]['elapsedRealtimeMillis']==102000
    assert 'synthetic-secret' not in contents(tmp_path)
    env=json.loads((tmp_path/'environment.json').read_text(encoding='utf-8'))
    assert env['sampling']['scope'].startswith('this_python_process')
    diag.close()


def test_timing_event_fields_are_bounded_and_never_copy_raw_words_or_buttons(tmp_path):
    diag=d.RuntimeDiagnostics(tmp_path,memory=lambda:{})
    diag.capture_state('ready');diag.record_timing('screenshot',2,outcome='succeeded')
    diag.record_timing('input',11,outcome='failed')
    diag.record_event(reason='password=synthetic-secret')
    for index in range(725):
        diag.record_event(reason='event_observed',event_id=1101,trusted=True,word_count=5,text_length=42,
            buttons=[dict(slot=0,box=[100,100,40,20],button_confidence=.9,enabled_confidence=.8,
                label='synthetic-secret',username='synthetic-user',password='synthetic-secret')])
    diag.close()
    rows=[json.loads(line) for line in (tmp_path/'state-history.ndjson').read_text(encoding='utf-8').splitlines()]
    assert len(rows)==720 and rows[-1]['cacheStatus']=='DISABLED_SAME_FRAME'
    assert rows[-1]['buttons']==[dict(slot=0,box=[100,100,40,20],button_confidence=.9,enabled_confidence=.8)]
    data=contents(tmp_path)
    assert 'synthetic-secret' not in data and 'synthetic-user' not in data
    env=json.loads((tmp_path/'environment.json').read_text(encoding='utf-8'))
    assert env['runtimeAtExport']['capture']['screenshotCount']==1


def test_existing_timings_forward_actual_input_completion_without_changing_aggregates(tmp_path):
    diag=d.RuntimeDiagnostics(tmp_path,memory=lambda:{})
    timings=Timings();timings.attach_diagnostics(diag)
    timings.record('input',12.5,outcome='unknown')
    timings.record('screenshot',2.5,outcome='succeeded')
    assert timings.payload()['input']['outcomes']=={'unknown':1}
    timings.attach_diagnostics(NS(record_timing=lambda *a,**k:(_ for _ in ()).throw(OSError('synthetic-secret'))))
    timings.record('input',3,outcome='succeeded')
    assert timings.payload()['input']['count']==2
    diag.close()
    rows=(tmp_path/'state-history.ndjson').read_text(encoding='utf-8').splitlines()
    assert json.loads(rows[0])['durationMillis']==12.5
    assert 'synthetic-secret' not in contents(tmp_path)


def test_sampler_failure_and_storage_failure_do_not_escape_into_business(tmp_path,monkeypatch):
    def fail():raise OSError('synthetic-secret')
    diag=d.RuntimeDiagnostics(tmp_path,memory=fail)
    row=diag.sample()
    assert row is not None and diag.issues=={'process_memory_unavailable'}
    monkeypatch.setattr(diag,'_write',lambda *a:fail())
    assert diag.sample() is None and 'sample_unavailable' in diag.issues
    diag.close()
    assert diag.closed
    assert 'synthetic-secret' not in contents(tmp_path)


def test_live_windows_sampler_stops_and_actual_memory_query_tears_down(tmp_path):
    memory=d.process_memory()
    assert all(type(value) is int and value>=0 for value in memory.values())
    diag=d.RuntimeDiagnostics(tmp_path,interval=.02)
    diag.start();first=diag.thread;diag.start()
    assert diag.thread is first
    until=time.monotonic()+2
    while len(diag.samples)<2 and time.monotonic()<until:time.sleep(.01)
    diag.capture_state('paused')
    diag.close();diag.close()
    assert not first.is_alive() and len(diag.samples)>=2
    assert json.loads((tmp_path/'environment.json').read_text(encoding='utf-8'))['runtimeAtExport']['capture']['state']=='paused'


def test_native_notifications_remain_live_when_trace_timings_are_deferred(tmp_path):
    from maa.event_sink import NotificationType as NT
    class Source:
        def add_sink(self,sink):return 1
        def add_context_sink(self,sink):return 2
        def remove_sink(self,number):pass
        def remove_context_sink(self,number):pass
    diag=d.RuntimeDiagnostics(tmp_path/'diagnostics',memory=lambda:{})
    timings=Timings();timings.attach_diagnostics(diag)
    tasker,controller=Source(),Source()
    collector=NativeTimings(tasker,controller,timings,trace_path=tmp_path/'maa/maafw.log')
    task=NS(task_id=1,entry='synthetic-secret')
    collector.task_sink.on_tasker_task(tasker,NT.Starting,task)
    collector.controller_sink.on_controller_action(controller,NT.Starting,NS(action='Screencap',ctrl_id=2))
    collector.context_sink.on_node_recognition(None,NT.Starting,NS(task_id=1,reco_id=3))
    row=diag.sample()
    assert row['capture']['taskNotification']=='starting'
    assert row['capture']['captureNotification']=='starting'
    assert row['capture']['recognitionNotification']=='starting'
    assert timings.payload()['screenshot'] is None
    collector.close();diag.close()
    assert 'synthetic-secret' not in contents(tmp_path/'diagnostics')


def event_fixture(diag):
    event=catalogue()['events'][0]
    text=event['name']+''.join(c['name']+c['description'] for c in event['choices'])
    words=[dict(text=text,box=[100,100,500,50])]
    image=np.zeros((720,1280,3),np.uint8)
    layout=next(item for item in catalogue()['layouts'] if item['optionCount']==len(event['choices']))
    for roi in layout['selectButtonRois']:
        x,y,w,h=pixel_rect(roi);image[y:y+h,x:x+w]=[255,150,50]
    context=NS(tasker=NS(_labyrinth_diagnostics=diag),run_recognition=lambda *a:NS(hit=True,raw_detail={'filtered':words}))
    argv=NS(image=image,custom_recognition_param='{}')
    return context,argv,event,words


def test_real_event_recognition_reports_stability_identity_and_disabled_button_reasons(tmp_path):
    diag=d.RuntimeDiagnostics(tmp_path,memory=lambda:{})
    context,argv,event,words=event_fixture(diag)
    observer=EventRecognition()
    first=observer.analyze(context,argv)
    assert first.detail['status']=='observed' and first.detail['trusted'] is False
    assert observer.analyze(context,argv).detail['trusted'] is True
    argv.custom_recognition_param=json.dumps(dict(event_id=int(event['id'])+1,slot=0))
    assert observer.analyze(context,argv).box is None
    argv.custom_recognition_param=json.dumps(dict(event_id=int(event['id']),slot=99))
    assert observer.analyze(context,argv).box is None
    # A visible blue button can remain too dim for a guarded click.
    argv.image[:]=[150,110,80]
    argv.custom_recognition_param=json.dumps(dict(event_id=int(event['id']),slot=0))
    assert observer.analyze(context,argv).box is None
    argv.image[:]=0
    assert observer.analyze(context,argv).box is None
    words[0]['text']='synthetic-secret-no-event'
    assert observer.analyze(context,argv).box is None
    diag.close()
    rows=[json.loads(line) for line in (tmp_path/'state-history.ndjson').read_text(encoding='utf-8').splitlines()]
    assert [row['reason'] for row in rows]==['event_observed','event_observed','event_identity_changed',
        'event_slot_invalid','event_button_disabled','event_button_absent','event_unmatched']
    assert rows[1]['trusted'] is True and rows[1]['stableFrames']==2
    assert all(row['ocrDurationMillis']>=0 and row['cacheStatus']=='DISABLED_SAME_FRAME' for row in rows)
    assert 'synthetic-secret' not in contents(tmp_path)


def test_event_diagnostics_cannot_change_guard_or_swallow_ocr_error(tmp_path):
    bad=NS(record_event=lambda **k:(_ for _ in ()).throw(RuntimeError('synthetic-secret')))
    context,argv,_,_=event_fixture(bad)
    observer=EventRecognition()
    assert observer.analyze(context,argv).detail['status']=='observed'
    context.run_recognition=lambda *a:(_ for _ in ()).throw(RuntimeError('ocr-unavailable'))
    with pytest.raises(RuntimeError,match='ocr-unavailable'):observer.analyze(context,argv)


@pytest.mark.parametrize('interval,limit',[(0,1),(float('nan'),1),(10,0),(10,361),(True,1)])
def test_invalid_sampling_configuration_is_rejected(tmp_path,interval,limit):
    with pytest.raises(ValueError):d.RuntimeDiagnostics(tmp_path,interval=interval,max_samples=limit)
