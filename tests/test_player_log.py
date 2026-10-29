import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
import threading
import time

from pcr_maa import player_log


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(player_log, '_state', None)
    yield
    player_log.close()


def test_disabled_diagnostics_do_not_touch_controller_or_stdout(capsys):
    player_log.text('disabled')
    player_log.observe({'nodes': []}, 'ObserveHome')
    player_log.capture(NS(), 'failed')
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('function', ['daily', 'clan', 'labyrinth'])
def test_safe_cached_picture_and_bounded_display_packet(tmp_path, function):
    rows = []
    run = 'a' * 32
    player_log.configure(tmp_path, run, function, rows.append)
    player_log.text('开始核对目标')
    player_log.observe({'nodes': [{'name': 'CBPage_battle'}], 'native_ok': True, 'task_id': 1}, 'CBPage_battle')
    frame = np.zeros((720, 1280, 3), np.uint8)
    player_log.capture(NS(controller=NS(cached_image=frame)), 'failed')
    player_log.flush()
    assert [r['event'] for r in rows] == ['text', 'native', 'image']
    assert all(r['run_id'] == run and r['function'] == function for r in rows)
    saved = tmp_path / '.runtime/player-logs' / function / run / rows[-1]['name']
    assert saved.is_file()
    assert sum(p.stat().st_size for p in saved.parent.iterdir()) <= player_log.QUOTA
    assert frame.shape == (720, 1280, 3)


@pytest.mark.parametrize('page', ['LabyClientTitleObserved', 'LabyClientProfileObserved', 'Captcha', 'Login'])
def test_login_and_identity_frames_are_not_saved(tmp_path, page):
    rows = []
    player_log.configure(tmp_path, 'a' * 32, 'daily', rows.append)
    player_log.observe({'nodes': [{'name': page}], 'native_ok': True}, page)
    player_log.capture(NS(controller=NS(cached_image=np.zeros((720, 1280, 3), np.uint8))), 'failed')
    player_log.flush()
    assert not list(tmp_path.rglob('*.png'))
    assert rows[-1]['event'] == 'text'


def test_diagnostic_sink_failure_does_not_change_business_execution(tmp_path):
    def broken(_): raise OSError('synthetic closed output')
    player_log.configure(tmp_path, 'b' * 32, 'clan', broken)
    player_log.text('info')
    player_log.observe({'nodes': [{'name': 'CBPage_battle'}]}, 'CBPage_battle')
    player_log.capture(NS(), 'failed')


@pytest.mark.parametrize('run,function', [('../escape', 'daily'), ('a'*32, 'other'), ('A'*32, 'clan')])
def test_identity_cannot_select_an_arbitrary_directory(tmp_path, run, function):
    with pytest.raises(ValueError, match='identity'):
        player_log.configure(tmp_path, run, function)


def test_alpha_packet_is_display_only_single_json_line(tmp_path, capsys):
    player_log.configure(tmp_path, 'c' * 32, 'daily')
    player_log.text('一行\n二行')
    player_log.flush()
    output = capsys.readouterr().out
    assert output.count('\n') == 1
    row = json.loads(output[len(player_log.PREFIX):])
    assert row['text'] == '一行\n二行'
    assert not {'ok', 'execute', 'grant', 'consumption_verified'} & row.keys()


def test_slow_logging_never_waits_in_native_business_path(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        release.wait(5)
    player_log.configure(tmp_path, 'd'*32, 'clan', slow)
    player_log.text('first')
    assert entered.wait(1)
    start=time.monotonic()
    try:
        for _ in range(1000):
            player_log.observe({'nodes':[{'name':'CBPage_battle'}]}, 'CBPage_battle')
        assert time.monotonic()-start < .5
        assert player_log._state['pending'].qsize() <= 128
    finally:
        release.set()


def test_picture_rotation_preserves_business_files_and_atomic_images(tmp_path):
    rows=[]
    player_log.configure(tmp_path,'e'*32,'clan',rows.append)
    player_log.observe({'nodes':[{'name':'CBPage_battle'}], 'native_ok': True},'CBPage_battle')
    ledger=tmp_path/'.runtime/clan-battle-daily-work/ledger/transactions.jsonl'
    ledger.parent.mkdir(parents=True)
    ledger.write_text('original unknown operation\n',encoding='utf-8')
    tasker=NS(controller=NS(cached_image=np.random.default_rng(1).integers(0,256,(720,1280,3),dtype=np.uint8)))
    for _ in range(8): player_log.capture(tasker,'failed')
    player_log.flush()
    area=tmp_path/'.runtime/player-logs/clan'
    pictures=list(area.rglob('*.png'))
    assert 1 <= len(pictures) < 8
    assert sum(p.stat().st_size for p in pictures) <= player_log.QUOTA
    assert not list(area.rglob('*.tmp'))
    assert ledger.read_text(encoding='utf-8') == 'original unknown operation\n'


def test_failed_observation_cannot_reuse_previous_safe_page(tmp_path):
    rows=[]
    player_log.configure(tmp_path,'e'*32,'daily',rows.append)
    player_log.observe({'nodes':[{'name':'DailyHome'}], 'native_ok':True},'DailyHome')
    player_log.observe({'nodes':[{'name':'DailyHome'}], 'native_ok':False},'DailyHome')
    player_log.capture(NS(controller=NS(cached_image=np.zeros((720,1280,3),np.uint8))),'failed')
    player_log.flush()
    assert not list(tmp_path.rglob('*.png'))
    assert rows[-1]['event'] == 'text'
