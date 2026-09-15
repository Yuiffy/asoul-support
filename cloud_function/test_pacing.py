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
        self.assertEqual(sent[1][0]-sent[0][0],40)
        params=urllib.parse.parse_qs(urllib.parse.urlsplit(sent[1][1].full_url).query)
        body=urllib.parse.parse_qs(sent[1][1].data.decode())
        self.assertEqual(params['wts'],['1040'])
        self.assertEqual(body['rnd'],['1040'])
        self.assertEqual(client.room.call_count,2)

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

    def test_sui_full_available_rounds_and_watch_finish_before_other_rooms(self):
        sequence=[]
        client=Mock(uid=123,watch_seconds=0)
        client.login.return_value={'uid':123}
        client.medal_rooms.return_value={1:11,cloud.SUI_ROOM:cloud.SUI_UID}
        client.room.return_value={'live_status':1}
        data={'task_info':[{'jump_type':'watchLive','sub_title':'每日上限 0/10'}]}
        def tasks(uid):
            sequence.append(('tasks',uid))
            return data if uid==cloud.SUI_UID else {'task_info':[],'reach_free_intimacy_limit':True}
        client.tasks.side_effect=tasks
        def free(*args):
            sequence.append(('free',args[1],args[6]))
            return args[3]
        def watch(*args):
            sequence.append(('watch_finished',args[2]))
            return {'task_info':[{'jump_type':'watchLive','sub_title':'每日上限 10/10'}]},9000
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='already_done'),patch('index.free_actions',side_effect=free),patch('index.queue_watch',side_effect=watch):
            metadata,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},cloud.today(),time.monotonic()+100,Ledger(),{})
        self.assertNotIn('error',metadata)
        self.assertIn(('free',cloud.SUI_ROOM,10),sequence)
        self.assertLess(sequence.index(('watch_finished',cloud.SUI_ROOM)),sequence.index(('tasks',11)))
        self.assertEqual(rows[0]['watch_seconds'],9000)
