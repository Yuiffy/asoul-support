import io
import json
import time
import unittest
import urllib.parse
from unittest.mock import Mock, patch

import index as cloud
from test_cloud import Ledger


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.on_sleep = None

    def sleep(self, seconds):
        if self.on_sleep:
            callback, self.on_sleep = self.on_sleep, None
            callback()
        self.now += seconds


class PacingTests(unittest.TestCase):
    @patch('index.time.sleep')
    def test_unchanged_counter_ends_after_bounded_remaining_like_batches(self,_):
        client=Mock()
        client.gate=cloud.AccountGate()
        client.room.return_value={'uid':11,'live_status':1}
        data={'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':'每日上限 5/10'}]}
        client.tasks.return_value=data
        audit={}
        cloud.free_actions(client,1,11,data,cloud.today(),time.monotonic()+60,10,audit)
        self.assertEqual(client.like.call_count,5)
        self.assertEqual(audit['like'],{'accepted':5,'accepted_clicks':150,'stop':'no_confirmed_progress'})
        cloud.free_actions(client,1,11,data,cloud.today(),time.monotonic()+60,1,audit)
        self.assertEqual(client.like.call_count,5)

    def test_offline_partial_likes_have_an_explicit_reason_and_no_post(self):
        client=Mock()
        client.gate=cloud.AccountGate()
        client.room.return_value={'uid':11,'live_status':0}
        data={'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':'每日上限 5/10'}]}
        audit={}
        cloud.free_actions(client,1,11,data,cloud.today(),time.monotonic()+60,10,audit)
        client.like.assert_not_called()
        self.assertEqual(audit['like'],{'accepted':0,'stop':'not_live'})

    def frequency_probe(self, rejections=1, deadline_seconds=300, live_after_wait=False,
                        risk_during_wait=False, task_full_after_wait=False):
        clock = Clock()
        client = cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt = 'test-salt'
        if risk_during_wait:
            clock.on_sleep = lambda:client.gate.rejected(-352,cloud.LIKE_ENDPOINT)
        sent = []
        accepted = []
        def send(request, **kwargs):
            sent.append(clock.now)
            code = 10031 if len(sent) <= rejections else 0
            if not code:
                accepted.append(clock.now)
            return io.BytesIO(json.dumps({'code':code,'data':{}}).encode())
        client.opener.open = Mock(side_effect=send)
        client.room = Mock(side_effect=lambda room:{'uid':11,
            'live_status':1 if live_after_wait and clock.now>=1060 else 0})
        def tasks():
            count = 1 if task_full_after_wait and clock.now>=1060 else len(accepted)
            return {'task_info':[{'jump_type':'sendDanmu','sub_title':f'每日上限 {count}/1'}]}
        client.tasks = Mock(side_effect=lambda uid:tasks())
        with patch('index.time.monotonic',side_effect=lambda:clock.now), patch('index.time.time',side_effect=lambda:clock.now), patch('index.time.sleep',side_effect=clock.sleep), patch('index.random.uniform',side_effect=lambda a,b:b):
            try:
                result = cloud.free_actions(client,1,11,tasks(),cloud.today(),clock.now+deadline_seconds,1)
                error = None
            except cloud.TaskError as exc:
                result, error = None, exc
        return client, sent, result, error, clock.now

    def test_frequency_rejection_waits_reconciles_and_then_succeeds(self):
        client, sent, result, error, _ = self.frequency_probe()
        self.assertIsNone(error)
        self.assertEqual(sent, [1000,1060])
        self.assertEqual(cloud.summary(result)['sendDanmu'],[1,1])
        self.assertEqual(client.tasks.call_count,2)
        self.assertFalse(client.gate.disabled)

    def test_repeated_frequency_rejections_have_account_wide_bounded_retries(self):
        client, sent, _, error, _ = self.frequency_probe(rejections=3)
        self.assertEqual(error.code,10031)
        self.assertEqual(sent,[1000,1060,1180])
        self.assertIn('/msg/send',client.gate.disabled)
        self.assertFalse(client.gate.stopped)

    def test_frequency_wait_cannot_overrun_deadline(self):
        client, sent, _, error, now = self.frequency_probe(deadline_seconds=45)
        self.assertIsNone(error)
        self.assertEqual(sent,[1000])
        self.assertEqual(now,1000)

    def test_room_that_goes_live_during_frequency_wait_is_not_sent_again(self):
        _, sent, _, error, _ = self.frequency_probe(live_after_wait=True)
        self.assertIsNone(error)
        self.assertEqual(sent,[1000])

    def test_account_risk_interrupts_frequency_wait_without_another_post(self):
        client, sent, _, error, now = self.frequency_probe(risk_during_wait=True)
        self.assertIsInstance(error,cloud.TaskError)
        self.assertTrue(client.gate.stopped)
        self.assertEqual(sent,[1000])
        self.assertLess(now,1060)

    def test_task_that_fills_during_frequency_wait_is_not_sent_again(self):
        _, sent, result, error, _ = self.frequency_probe(task_full_after_wait=True)
        self.assertIsNone(error)
        self.assertEqual(sent,[1000])
        self.assertEqual(cloud.summary(result)['sendDanmu'],[1,1])

    def test_like_cooldown_spans_rooms_and_queries_do_not_reset_it(self):
        for choose in (min, max):
            clock, gate = Clock(), cloud.AccountGate()
            sent = []
            client = cloud.Bili('SESSDATA=example; bili_jct=example',123,gate)
            client.salt = 'test-salt'
            def send(request, **kwargs):
                sent.append((clock.now,request))
                return io.BytesIO(b'{"code":0,"data":{}}')
            client.opener.open = Mock(side_effect=send)
            with patch('index.time.monotonic',side_effect=lambda:clock.now), patch('index.time.time',side_effect=lambda:clock.now), patch('index.time.sleep',side_effect=clock.sleep), patch('index.random.uniform',side_effect=lambda a,b:choose(a,b)):
                client.like(1,11,30)
                client.room(2)
                client.like(3,33,30)
            self.assertEqual(sent[2][0]-sent[0][0], choose(15,20))
            self.assertLess(sent[1][0],sent[2][0])
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(sent[2][1].full_url).query)
            self.assertEqual(int(params['wts'][0]), int(sent[2][0]))

    def test_cooldown_wait_releases_lock_for_other_requests(self):
        clock, gate = Clock(), cloud.AccountGate()
        observed = []
        with patch('index.time.monotonic',side_effect=lambda:clock.now), patch('index.time.sleep',side_effect=clock.sleep), patch('index.random.uniform',side_effect=lambda a,b:a):
            with gate.slot(cloud.LIKE_ENDPOINT):
                pass
            def heartbeat_during_wait():
                with gate.slot('/xlive/data-interface/v1/x25Kn/X'):
                    observed.append(clock.now)
            clock.on_sleep = heartbeat_during_wait
            with gate.slot(cloud.LIKE_ENDPOINT):
                second = clock.now
        self.assertTrue(observed)
        self.assertLess(observed[0],second)
        self.assertGreaterEqual(second,1015)

    def test_danmaku_wait_retains_offline_check_and_fresh_signature(self):
        clock = Clock()
        client = cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt = 'test-salt'
        client.room = Mock(return_value={'live_status':0})
        sent=[]
        def send(request,**kwargs):
            sent.append((clock.now,request))
            return io.BytesIO(b'{"code":0,"data":{}}')
        client.opener.open=Mock(side_effect=send)
        with patch('index.time.monotonic',side_effect=lambda:clock.now), patch('index.time.time',side_effect=lambda:clock.now), patch('index.time.sleep',side_effect=clock.sleep), patch('index.random.uniform',side_effect=lambda a,b:b):
            client.danmu(1,'test')
            client.danmu(2,'test')
        self.assertEqual(sent[1][0]-sent[0][0],8)
        params=urllib.parse.parse_qs(urllib.parse.urlsplit(sent[1][1].full_url).query)
        body=urllib.parse.parse_qs(sent[1][1].data.decode())
        self.assertEqual(params['wts'],['1008'])
        self.assertEqual(body['rnd'],['1008'])
        self.assertEqual(client.room.call_count,2)

    def test_ten_offline_messages_finish_in_two_minutes_without_bursting(self):
        clock = Clock()
        client = cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt = 'test-salt'
        client.room = Mock(return_value={'uid':11,'live_status':0})
        sent = []
        def send(request, **kwargs):
            sent.append(clock.now)
            return io.BytesIO(b'{"code":0,"data":{}}')
        client.opener.open = Mock(side_effect=send)
        def tasks():
            return {'task_info':[{'jump_type':'sendDanmu','title':'发弹幕1次',
                                  'sub_title':f'每日上限 {len(sent)}/10'}]}
        client.tasks = Mock(side_effect=lambda uid:tasks())
        with patch('index.time.monotonic',side_effect=lambda:clock.now), patch('index.time.time',side_effect=lambda:clock.now), patch('index.time.sleep',side_effect=clock.sleep), patch('index.random.uniform',side_effect=lambda a,b:b):
            result = cloud.free_actions(client,1,11,tasks(),cloud.today(),clock.now+120,10)
        self.assertEqual(len(sent),10)
        self.assertEqual(cloud.summary(result)['sendDanmu'],[10,10])
        self.assertLessEqual(clock.now-1000,120)
        self.assertTrue(all(b-a>=6 for a,b in zip(sent,sent[1:])))
        self.assertEqual(client.room.call_count,20)

    @patch('index.time.sleep')
    def test_one_api_request_per_like_round(self,_):
        client=Mock()
        client.gate=cloud.AccountGate()
        client.room.return_value={'uid':cloud.SUI_UID,'live_status':1}
        data={'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':'每日上限 0/10'}]}
        client.tasks.return_value={'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':'每日上限 1/10'}]}
        cloud.free_actions(client,cloud.SUI_ROOM,cloud.SUI_UID,data,cloud.today(),time.monotonic()+60,1)
        client.like.assert_called_once_with(cloud.SUI_ROOM,cloud.SUI_UID,30)

    def test_risk_signal_interrupts_an_already_waiting_like(self):
        clock,gate=Clock(),cloud.AccountGate()
        with patch('index.time.monotonic',side_effect=lambda:clock.now),patch('index.time.sleep',side_effect=clock.sleep),patch('index.random.uniform',side_effect=lambda a,b:a):
            with gate.slot(cloud.LIKE_ENDPOINT):
                pass
            clock.on_sleep=lambda:gate.rejected(-352,cloud.LIKE_ENDPOINT)
            with self.assertRaises(cloud.TaskError):
                with gate.slot(cloud.LIKE_ENDPOINT):
                    self.fail('Must not enter request after risk rejection')
        self.assertFalse(gate.lock.locked())

    def test_sui_full_available_rounds_before_other_room_interactions(self):
        sequence=[]
        client=Mock(uid=123,watch_seconds=0)
        client.live_statuses.return_value={}
        client.login.return_value={'uid':123}
        client.medal_rooms.return_value={1:11,cloud.SUI_ROOM:cloud.SUI_UID}
        client.room.return_value={'live_status':1}
        data={'task_info':[{'jump_type':'watchLive','sub_title':'每日上限 0/10'}]}
        def tasks(uid):
            sequence.append(('tasks',uid))
            return data if uid==cloud.SUI_UID else {'task_info':[],'reach_free_intimacy_limit':True}
        client.tasks.side_effect=tasks
        def free(*args, **kwargs):
            sequence.append(('free',args[1],args[6]))
            return args[3]
        def watch(*args):
            sequence.append(('watch_finished',args[2]))
            return {'task_info':[{'jump_type':'watchLive','sub_title':'每日上限 10/10'}]},9000
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='already_done'),patch('index.free_actions',side_effect=free),patch('index.queue_watch',side_effect=watch):
            metadata,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},cloud.today(),time.monotonic()+100,Ledger(),{})
        self.assertNotIn('error',metadata)
        self.assertIn(('free',cloud.SUI_ROOM,10),sequence)
        self.assertLess(sequence.index(('free',cloud.SUI_ROOM,10)),sequence.index(('tasks',11)))
        self.assertEqual(rows[0]['watch_seconds'],9000)
