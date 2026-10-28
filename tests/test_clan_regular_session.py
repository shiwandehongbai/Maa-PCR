"""One ordinary preparation retains an actual empty-stock observation under the same Owner."""
from copy import deepcopy
from datetime import timedelta

import pytest
from pcr_maa import clan_battle_batch as batch
from test_clan_battle_batch import daily_request, Port, run, ledgers


class SessionPort(Port):
    def prepare(self, action, job, binding):
        ready=super().prepare(action,job,binding)
        self.clock += timedelta(seconds=180)
        ready['evidence']['prepared_at']=self.clock.isoformat()
        self.session_before=deepcopy(self.observations[-1])
        self.session_ready=deepcopy(ready)
        return ready
    def prepared_regular_observation(self,before,ready):
        assert before==self.session_before and ready==self.session_ready
        assert before['carry_stock_indices']==[] and ready['support_unit'] is None
        assert ready['kind']=='regular'
        return True


def test_three_minute_preparation_happens_once_and_keeps_original_inventory_time(tmp_path,daily_request):
    port=SessionPort(daily_request)
    result,_=run(tmp_path,daily_request,port)
    assert result['outcome']=='complete',result
    assert len(port.prepared)==3 and len(port.executions)==3
    for intent in port.executions:
        assert intent['observed_at']==intent['before']['observed_at']
        assert batch._timestamp(intent['ready']['evidence']['prepared_at'])-batch._timestamp(intent['observed_at'])==timedelta(seconds=180)
        assert intent['before'] in port.observations


from test_clan_consumption_consent import ConsentPort
from pcr_maa.clan_battle_daily_native import NativeClanDailyAdapter


class ConsentSessionPort(SessionPort, ConsentPort):
    def prepare(self,action,job,binding):
        ready=super().prepare(action,job,binding)
        self._regular_prepared=(deepcopy(self.session_before),deepcopy(ready))
        return ready
    def execute(self,intent,job):
        # Exercise the real native comparison after the real batch appended permission.
        permission=intent['ready']['evidence']['consumption_authorization']
        assert permission['reserved_intent_id']==intent['intent_id']
        assert NativeClanDailyAdapter._regular_ready_at(self,intent['before'],intent['ready'])==self.session_ready['evidence']['prepared_at']
        return super().execute(intent,job)


def test_authorized_batch_executes_once_with_original_before_and_appended_permission(tmp_path,daily_request):
    port=ConsentSessionPort(daily_request)
    result,_=run(tmp_path,daily_request,port)
    assert result['outcome']=='complete',result
    assert len(port.prepared)==len(port.executions)==3 and len(port.confirmations)==1
    for intent in port.executions:
        assert intent['observed_at']==intent['before']['observed_at']
        assert intent['ready']['evidence']['consumption_authorization']['preview']['actions'][intent['action']['slot']-1]['instance_id']==intent['action']['instance_id']


def test_retained_path_does_not_accept_an_approval_for_a_different_instance(tmp_path,daily_request):
    port=ConsentSessionPort(daily_request);port.consent_fault='mutate_preview'
    result,_=run(tmp_path,daily_request,port)
    assert 'authorization_invalid' in result['error']
    assert not port.executions and not port.prepared and not ledgers(tmp_path)


def test_retained_unknown_start_remains_unresolved_and_is_never_repeated(tmp_path,daily_request):
    port=ConsentSessionPort(daily_request);port.fail='unknown_after_start'
    first,_=run(tmp_path,daily_request,port)
    assert first['error']=='unknown_after_start' and len(port.executions)==1
    second,_=run(tmp_path,daily_request,port)
    assert second['error']=='daily_previous_result_unknown'
    assert len(port.executions)==len(port.confirmations)==len(port.prepared)==1
