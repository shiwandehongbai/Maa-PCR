"""MP4 capture lifecycle and narrow metadata tests; all ADB calls are fake."""
import hashlib
import json
from pathlib import Path
import re
import struct
import time
from types import SimpleNamespace

import pytest
from pcr_maa import clan_battle_opening_observation as module


def box(kind, payload):
    return struct.pack('>I4s',8+len(payload),kind)+payload


def sample_mp4(*, version=1, outside=False, times=(1000000,1010000), chunks=(1,1,1,1)):
    if version==1:
        sample=b'#VV1NSC0PET1ME!#'+struct.pack('<I',len(times))+struct.pack('<'+'Q'*len(times),*times)
    else:
        sample=b'#VV1NSC0PET1ME2#'+struct.pack('<IqQ',2,-1000000000000,len(times))+struct.pack('<'+'Q'*len(times),*times)
    ftyp=box(b'ftyp',b'isom\0\0\0\0isom')
    offset=0 if outside else len(ftyp)+8
    stsd=box(b'stsd',b'\0'*4+struct.pack('>I',1)+box(b'mett',b'\0'*8))
    stsz=box(b'stsz',b'\0'*4+struct.pack('>III',0,1,len(sample)))
    stsc=box(b'stsc',b'\0'*4+struct.pack('>4I',*chunks))
    stco=box(b'stco',b'\0'*4+struct.pack('>II',1,offset))
    stbl=box(b'stbl',stsd+stsz+stsc+stco)
    moov=box(b'moov',box(b'trak',box(b'mdia',box(b'minf',stbl))))
    return ftyp+box(b'mdat',sample)+moov


@pytest.mark.parametrize('version,multiplier',[(1,1000),(2,1)])
def test_metadata_comes_from_single_sample_tables_without_ffmpeg(tmp_path,version,multiplier):
    path=tmp_path/'record.mp4'
    path.write_bytes(sample_mp4(version=version))
    result=module.read_screenrecord_metadata(path)
    assert result['source']=='mp4_mett_tables' and result['version']==version
    assert result['elapsed_ns']==[1000000*multiplier,1010000*multiplier]
    assert result['timestamp_semantics_verified'] is False


@pytest.mark.parametrize('kwargs,reason',[
    ({'outside':True},'outside_mdat'),
    ({'times':(1000000,1000000)},'time_order'),
    ({'chunks':(1,1,2,1)},'chunk_mapping'),
])
def test_unbound_or_ambiguous_metadata_stops(tmp_path,kwargs,reason):
    path=tmp_path/'record.mp4';path.write_bytes(sample_mp4(**kwargs))
    with pytest.raises(ValueError,match=reason):
        module.read_screenrecord_metadata(path)


def fake_capture(tmp_path,monkeypatch,mode='natural'):
    media=sample_mp4()
    adb=tmp_path/'adb.exe';adb.write_bytes(b'not an executable; Popen is patched')
    holder={'calls':[],'processes':[],'stats':0,'record':None}
    class Process:
        def __init__(self,argv,stdin,stdout,stderr,creationflags):
            assert argv[:3]==[str(adb),'-s','emulator-5554']
            args=argv[3:];self.arguments=args;holder['calls'].append(args);holder['processes'].append(self)
            self.pid=10000+len(holder['calls']);self.record=False;self.done=True;self.returncode=0;self.polls=0
            self.killed=False;data=b''
            if args[:4]==['shell','test','!','-e']:
                self.returncode=1 if mode=='path_exists' else 0
            elif args==['shell','cat','/proc/uptime']:
                data=b'1000.00 500.00\n'
            elif args[:3]==['shell','sha256sum','/system/bin/screenrecord']:
                data=b'1'*64+b'  /system/bin/screenrecord\n'
            elif args[0]=='shell' and args[1].startswith("sh -c '"):
                assert '--bugreport --display-id 4619827767814508545' in args[1]
                self.record=True;self.done=False;self.returncode=None;holder['record']=self
                remote=re.search(r'/sdcard/maa-clan-opening-[0-9a-f]{32}\.mp4',args[1]).group()
                holder['remote']=remote
                holder['argv']=[b'/system/bin/screenrecord',b'--bugreport',b'--display-id',b'4619827767814508545',
                    b'--size',b'1920x1080',b'--bit-rate',b'12000000',b'--time-limit',b'2',remote.encode('ascii')]
                data=b'MAA_SCREENRECORD_PID:73\n'
            elif args[:2]==['exec-out','cat'] and args[-1].endswith('/stat'):
                holder['stats']+=1
                start=101 if mode=='identity_changed' and holder['stats']>1 else 100
                fields=['S']+['0']*18+[str(start)]+['0']*5
                data=('73 (screenrecord) '+' '.join(fields)).encode('ascii')
            elif args[:2]==['exec-out','cat'] and args[-1].endswith('/cmdline'):
                data=b'game-process\0' if mode=='wrong_cmdline' else b'\0'.join(holder['argv'])+b'\0'
            elif args[:3]==['shell','kill','-INT']:
                assert args==['shell','kill','-INT','73']
                holder['record'].done=True;holder['record'].returncode=0
            elif args[:3]==['shell','stat','-c']:
                data=str(len(media)).encode('ascii')+b'\n'
            elif args[:2]==['shell','sha256sum']:
                digest='0'*64 if mode=='bad_hash' else hashlib.sha256(media).hexdigest()
                data=(digest+'  '+holder['remote']+'\n').encode('ascii')
            elif args[0]=='pull':
                Path(args[2]).write_bytes(media)
            else:
                raise AssertionError('unapproved command '+repr(args))
            stdout.write(data);stdout.flush();stderr.flush()
        def poll(self):
            if self.record and not self.done:
                self.polls+=1
                if self.polls>=5:
                    self.done=True;self.returncode=0
            return self.returncode if self.done else None
        def wait(self,timeout):
            if mode=='pull_timeout' and self.arguments[0]=='pull' and not self.killed:
                raise module.subprocess.TimeoutExpired('mock-local-pull',timeout)
            if self.record and not self.done:
                raise AssertionError('wait called without record exit')
            return self.returncode
        def kill(self):
            self.killed=True;self.done=True;self.returncode=-9
    monkeypatch.setattr(module.subprocess,'Popen',Process)
    capture=module.OpeningMp4Materials(adb,'emulator-5554','4619827767814508545',tmp_path/('a'*32),max_seconds=2,pre_roll_seconds=0)
    return capture,holder


def test_capture_uses_only_owned_pid_then_releases_and_pulls_original(tmp_path,monkeypatch):
    capture,holder=fake_capture(tmp_path,monkeypatch)
    capture.start()
    assert capture.identity is not None
    capture.mark('dispatch','CBStart','unknown')
    result=capture.finish(source_valid=True,battle_start='sent')
    assert result['status']=='captured' and result['local_worker_released'] is True
    assert result['first_frame_verified'] is False and result['coverage']=='unknown'
    assert result['not_fired_unit_ids']==[] and result['first_ub_verified'] is False
    assert result['metadata']['frame_count']==2 and result['media']['remote_original_retained'] is True
    assert result['markers'][0]['meaning']=='business_call_boundary_not_capture_pts'
    assert not any(args[:2]==['shell','kill'] for args in holder['calls'])
    assert result['remote_signals_sent'] is False and result['total_worker_deadline_seconds']==22
    assert all(not process.killed for process in holder['processes'])
    before=len(holder['calls']);capture.stop_and_join()
    assert len(holder['calls'])==before


def test_natural_limit_does_not_signal_remote_process(tmp_path,monkeypatch):
    capture,holder=fake_capture(tmp_path,monkeypatch,'natural')
    capture.start();capture.thread.join(timeout=2)
    assert not capture.thread.is_alive()
    result=capture.finish(source_valid=True,battle_start='sent')
    assert result['status']=='captured'
    assert not any(args[:2]==['shell','kill'] for args in holder['calls'])


@pytest.mark.parametrize('mode',['bad_hash','pull_timeout'])
def test_unknown_capture_never_issues_ub_proof_or_signals_a_reused_pid(tmp_path,monkeypatch,mode):
    capture,holder=fake_capture(tmp_path,monkeypatch,mode)
    capture.start()
    result=capture.finish(source_valid=True,battle_start='unknown')
    assert result['status']=='unknown' and result['not_fired_unit_ids']==[]
    assert result['first_ub_verified'] is False and result['local_worker_released'] is True
    assert not any(args[:2]==['shell','kill'] for args in holder['calls'])
    if mode=='pull_timeout':
        assert holder['processes'][-1].killed is True  # Only this Popen-owned local adb was released.


@pytest.mark.parametrize('mode',['path_exists','wrong_cmdline'])
def test_prestart_process_proof_failure_releases_without_start_retry(tmp_path,monkeypatch,mode):
    capture,holder=fake_capture(tmp_path,monkeypatch,mode)
    with pytest.raises(RuntimeError,match='process_not_ready'):
        capture.start()
    assert not capture.thread.is_alive()
    assert not any(args[:2]==['shell','kill'] for args in holder['calls'])
    assert sum(args[0]=='shell' and len(args)>1 and args[1].startswith("sh -c '") for args in holder['calls']) <= 1


def test_source_revocation_preserves_material_but_never_reports_bound_capture(tmp_path,monkeypatch):
    capture,_=fake_capture(tmp_path,monkeypatch)
    capture.start();result=capture.finish(source_valid=False,battle_start='sent')
    assert result['status']=='unknown' and result['source_bound'] is False
    assert result['media'] and result['not_fired_unit_ids']==[]


@pytest.mark.parametrize('requested,wanted',[(None,'png'),('','png'),('png','png'),('mp4','mp4')])
def test_native_capture_format_is_trusted_env_only_and_png_default(tmp_path,monkeypatch,requested,wanted):
    from pcr_maa import clan_battle_native as native_module
    monkeypatch.setattr(native_module.native,'ROOT',tmp_path)
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_ONLY','1')
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_DIR',str(tmp_path/'.runtime/capture'))
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_DISPLAY_ID','4619827767814508545')
    if requested is None:monkeypatch.delenv('PCR_CLAN_OPENING_CAPTURE_FORMAT',raising=False)
    else:monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_FORMAT',requested)
    calls=[]
    class Fake:
        def __init__(self,*args):self.directory=Path(args[-1]);calls.append(self.kind)
        def start(self):return self
    class Png(Fake):kind='png'
    class Mp4(Fake):kind='mp4'
    monkeypatch.setattr(module,'OpeningCaptureMaterials',Png)
    monkeypatch.setattr(module,'OpeningMp4Materials',Mp4)
    port=object.__new__(native_module.NativeClanBattle)
    port.plan={'capture_format':'opposite_task_json_must_be_ignored'}
    port._opening_capture_source=lambda: ('same_owner','trusted_adb','emulator-5554')
    port._start_opening_capture('a'*32)
    assert calls==[wanted] and port._opening_capture.directory.name=='a'*32


def test_invalid_env_format_rejected_without_creating_collector(tmp_path,monkeypatch):
    from pcr_maa import clan_battle_native as native_module
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_ONLY','1')
    monkeypatch.setenv('PCR_CLAN_OPENING_CAPTURE_FORMAT','invalid')
    port=object.__new__(native_module.NativeClanBattle)
    with pytest.raises(RuntimeError,match='capture_format_invalid'):
        port._start_opening_capture('a'*32)


def test_daily_uses_same_collector_factory_and_finish():
    from pcr_maa.clan_battle_daily_native import NativeClanDailyAdapter
    from pcr_maa.clan_battle_native import NativeClanBattle
    assert NativeClanDailyAdapter._start_opening_capture is NativeClanBattle._start_opening_capture
    assert NativeClanDailyAdapter.finish_opening_capture is NativeClanBattle.finish_opening_capture


def test_pre_start_warm_up_is_recorded_but_never_claims_first_frame(tmp_path,monkeypatch):
    capture,_=fake_capture(tmp_path,monkeypatch)
    capture.pre_roll_seconds=.08
    capture.start()
    assert capture.pre_roll_completed_ns-capture.process_verified_ns >= 80000000
    result=capture.finish(source_valid=True,battle_start='sent')
    assert result['pre_roll']['first_frame_proof'] is False
    assert result['first_frame_verified'] is False and result['coverage']=='unknown'


def test_local_original_is_preserved_without_pull_overwrite(tmp_path,monkeypatch):
    capture,holder=fake_capture(tmp_path,monkeypatch)
    capture.start()
    capture.media.write_bytes(b'preserve existing local original')
    result=capture.finish(source_valid=True,battle_start='sent')
    assert result['status']=='unknown' and 'local_media_exists' in result['error']
    assert capture.media.read_bytes()==b'preserve existing local original'
    assert not any(args[0]=='pull' for args in holder['calls'])


def test_finish_retains_handle_after_join_failure_and_retries_only_cleanup(tmp_path):
    from pcr_maa.clan_battle_native import NativeClanBattle
    class Capture:
        def __init__(self):self.calls=0;self.released=False;self.directory=tmp_path/'capture'
        def stop_and_join(self):
            self.calls+=1
            if self.calls==1:raise RuntimeError('worker_not_released')
            self.released=True
        def finish(self,**kwargs):
            assert self.released
            return dict(status='unknown',source_bound=kwargs['source_valid'],format='mp4',media=None)
    capture=Capture()
    port=object.__new__(NativeClanBattle)
    port._opening_capture=capture;port._opening_capture_expected='same-owner';port.directory=tmp_path
    port.battle_start='unknown'
    def source():
        assert capture.released
        return 'same-owner'
    port._opening_capture_source=source
    with pytest.raises(RuntimeError,match='worker_not_released'):
        port.finish_opening_capture()
    assert port._opening_capture is capture
    result=port.finish_opening_capture()
    assert result['status']=='unknown' and port._opening_capture is None
    assert capture.calls==2 and port.finish_opening_capture() is None
