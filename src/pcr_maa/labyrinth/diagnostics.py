"""Bounded scalar diagnostics for one owner; never retains images or login data."""
from collections import deque
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import threading
import time
from uuid import uuid4


def process_start_elapsed():
    if os.name != 'nt':return None
    try:
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=(wintypes.HANDLE,)+(ctypes.POINTER(wintypes.FILETIME),)*4
        creation,exit_time,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(),*[ctypes.byref(v) for v in (creation,exit_time,kernel_time,user_time)]):return None
        wall=((creation.dwHighDateTime<<32)|creation.dwLowDateTime)/10_000_000-11_644_473_600
        return max(0,time.monotonic()-(time.time()-wall))
    except Exception:return None


def process_memory():
    """This Python process and Windows host RAM, not the game or emulator."""
    if os.name != 'nt':return {}
    from ctypes import wintypes
    class ProcessMemory(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
            (name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize',
            'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage','PrivateUsage')]
    class HostMemory(ctypes.Structure):
        _fields_=[('dwLength',wintypes.DWORD),('dwMemoryLoad',wintypes.DWORD)]+[
            (name,ctypes.c_ulonglong) for name in ('ullTotalPhys','ullAvailPhys',
            'ullTotalPageFile','ullAvailPageFile','ullTotalVirtual','ullAvailVirtual','ullAvailExtendedVirtual')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=(wintypes.HANDLE,ctypes.POINTER(ProcessMemory),wintypes.DWORD)
    kernel.GlobalMemoryStatusEx.argtypes=(ctypes.POINTER(HostMemory),)
    result={};process=ProcessMemory();process.cb=ctypes.sizeof(process)
    if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(process),process.cb):
        result.update(processWorkingSetBytes=process.WorkingSetSize,processPrivateBytes=process.PrivateUsage)
    host=HostMemory();host.dwLength=ctypes.sizeof(host)
    if kernel.GlobalMemoryStatusEx(ctypes.byref(host)):
        result.update(hostTotalMemoryBytes=host.ullTotalPhys,hostAvailableMemoryBytes=host.ullAvailPhys)
    return result


class RuntimeDiagnostics:
    interval_seconds=10
    max_samples=360
    max_states=720
    capture_states=frozenset(('uninitialized','connecting','ready','paused','stopped','failed'))
    event_reasons=frozenset(('stopped','invalid_image','ocr_unavailable','ocr_empty',
        'event_unmatched','layout_unavailable','event_button_absent','event_unstable',
        'event_identity_changed','event_slot_invalid','event_button_disabled','event_observed',
        'event_guard_accepted','decision_selected','decision_unknown','choices_exhausted','choice_timeout'))
    memory_fields=frozenset(('processWorkingSetBytes','processPrivateBytes','hostTotalMemoryBytes','hostAvailableMemoryBytes'))

    def __init__(self,directory,*,interval=10,max_samples=360,clock=time.monotonic,
                 cpu_clock=time.process_time,wall_clock=time.time,memory=process_memory):
        if type(interval) not in (int,float) or not math.isfinite(interval) or interval<=0:
            raise ValueError('invalid_diagnostic_interval')
        if type(max_samples) is not int or not 1<=max_samples<=360:raise ValueError('invalid_diagnostic_limit')
        self.directory=Path(directory).absolute();self.interval_seconds=interval;self.max_samples=max_samples
        self.clock,self.cpu_clock,self.wall_clock,self.memory=clock,cpu_clock,wall_clock,memory
        self.started_at=clock();self.process_started_at=process_start_elapsed()
        self.previous=None;self.samples=deque(maxlen=max_samples);self.states=deque(maxlen=self.max_states)
        self.capture={'state':'uninitialized','screenshotCount':0};self.issues=set()
        self.lock=threading.RLock();self.write_lock=threading.Lock();self.stop=threading.Event();self.thread=None;self.closed=False

    def _timestamp(self):
        return dict(timestamp=int(self.wall_clock()*1000),elapsedRealtimeMillis=int(self.clock()*1000))

    def start(self):
        with self.lock:
            if self.thread is not None or self.closed:return
            self.sample()
            self.thread=threading.Thread(target=self._run,name='labyrinth-runtime-diagnostics',daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop.wait(self.interval_seconds):self.sample()

    def capture_state(self,state):
        if state not in self.capture_states:raise ValueError('invalid_capture_state')
        with self.lock:
            if not self.closed:self.capture['state']=state

    def record_native(self,kind,status):
        if kind not in ('capture','task','recognition','input') or status not in ('starting','succeeded','failed'):return
        with self.lock:
            if not self.closed:
                self.capture[kind+'Notification']=status
                self.capture[kind+'NotificationElapsedRealtimeMillis']=int(self.clock()*1000)

    def record_timing(self,category,elapsed_ms,*,outcome=None):
        if category not in ('screenshot','input'):return
        if type(elapsed_ms) not in (int,float) or not math.isfinite(elapsed_ms) or elapsed_ms<0:return
        if outcome not in (None,'succeeded','failed','unknown'):return
        with self.lock:
            if self.closed:return
            if category=='screenshot':
                self.capture.update(lastScreenshotMetricRecordedElapsedRealtimeMillis=int(self.clock()*1000),
                    lastScreenshotDurationMillis=elapsed_ms,lastScreenshotOutcome=outcome,
                    screenshotCount=self.capture['screenshotCount']+1)
            else:self.states.append(dict(self._timestamp(),kind='input_callback',durationMillis=elapsed_ms,outcome=outcome))

    def record_event(self,*,reason,ocr_ms=None,word_count=None,text_length=None,event_id=None,
                     score=None,margin=None,stable_frames=None,trusted=None,buttons=(),slot=None):
        # Only explicitly named scalar fields cross this diagnostic boundary.
        if reason not in self.event_reasons:return
        row=dict(self._timestamp(),kind='event',reason=reason,cacheStatus='DISABLED_SAME_FRAME',crop=[75,65,1130,500])
        for key,value in (('ocrDurationMillis',ocr_ms),('matchScore',score),('matchMargin',margin)):
            if type(value) in (int,float) and math.isfinite(value) and value>=0:row[key]=value
        for key,value,limit in (('wordCount',word_count,10000),('textLength',text_length,100000),
                ('eventId',event_id,99999),('stableFrames',stable_frames,100000),('slot',slot,3)):
            if type(value) is int and 0<=value<=limit:row[key]=value
        if type(trusted) is bool:row['trusted']=trusted
        safe=[]
        for button in tuple(buttons)[:4]:
            if type(button) is not dict:continue
            item={}
            for key in ('button_confidence','enabled_confidence'):
                value=button.get(key)
                if type(value) in (int,float) and math.isfinite(value) and 0<=value<=1:item[key]=value
            box=button.get('box')
            if type(box) is list and len(box)==4 and all(type(n) is int and 0<=n<=1280 for n in box):item['box']=list(box)
            if type(button.get('slot')) is int and 0<=button['slot']<4:item['slot']=button['slot']
            if item:safe.append(item)
        if safe:row['buttons']=safe
        with self.lock:
            if not self.closed:self.states.append(row)

    def sample(self):
        with self.lock:
            if self.closed:return None
            try:
                now,cpu=self.clock(),self.cpu_clock();previous=self.previous
                interval=now-previous[0] if previous else None;delta=cpu-previous[1] if previous else None
                row=dict(self._timestamp(),pid=os.getpid(),collectorStartElapsedRealtimeMillis=int(self.started_at*1000),
                    processStartElapsedRealtimeMillis=int(self.process_started_at*1000) if self.process_started_at is not None else None,
                    processAgeMillis=max(0,int((now-self.process_started_at)*1000)) if self.process_started_at is not None else None,processCpuMillis=cpu*1000,
                    sampleIntervalMillis=interval*1000 if interval is not None else None,
                    processCpuPercentOfOneCore=delta*100/interval if interval and interval>0 and delta>=0 else None,
                    capture=dict(self.capture))
                self.previous=(now,cpu)
                try:
                    row.update({key:value for key,value in self.memory().items()
                        if key in self.memory_fields and type(value) is int and value>=0})
                except Exception:self.issues.add('process_memory_unavailable')
                self.samples.append(row)
            except Exception:
                self.issues.add('sample_unavailable');return None
        try:
            self._flush();return row
        except Exception:
            with self.lock:self.issues.add('sample_unavailable')
            return None

    def _write(self,name,value):
        self.directory.mkdir(parents=True,exist_ok=True)
        for part in (self.directory,*self.directory.parents):
            if part.is_symlink() or part.is_junction():raise OSError('diagnostic_path_contains_link')
        target=self.directory/name
        if target.is_symlink() or target.is_junction():raise OSError('diagnostic_path_contains_link')
        temporary=self.directory/('.'+name+'.'+uuid4().hex+'.tmp')
        try:
            with temporary.open('x',encoding='utf-8',newline='\n') as stream:stream.write(value)
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)

    def _flush(self):
        with self.lock:
            samples=list(self.samples);states=list(self.states);issues=sorted(self.issues)
        environment=dict(schemaVersion=1,pythonVersion=platform.python_version(),
            platform=dict(system=platform.system(),release=platform.release(),version=platform.version(),machine=platform.machine()),
            sampling=dict(intervalMillis=self.interval_seconds*1000,maxSamples=self.max_samples,maxStates=self.max_states,
                scope='this_python_process_and_windows_host_memory; game_and_emulator_metrics_unavailable'),
            issues=issues,runtimeAtExport=samples[-1] if samples else None)
        encode=lambda value:json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        with self.write_lock:
            self._write('environment.json',encode(environment)+'\n')
            self._write('runtime-history.ndjson',''.join(encode(row)+'\n' for row in samples))
            self._write('state-history.ndjson',''.join(encode(row)+'\n' for row in states))
            self._write('state-latest.json',encode(states[-1] if states else None)+'\n')

    def close(self):
        self.stop.set()
        if self.thread is not None and self.thread is not threading.current_thread():self.thread.join(timeout=2)
        with self.lock:
            if self.closed:return
            if self.thread is not None and self.thread.is_alive():self.issues.add('sampler_shutdown_incomplete')
        self.sample()
        with self.lock:self.closed=True
