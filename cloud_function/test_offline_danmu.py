import io
import json
import time
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch
import index as cloud


class OfflineDanmuTests(unittest.TestCase):
    def grouped_client(self, statuses=(0, 0)):
        client=cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt='test-salt'
        client.gate.wait_action=Mock()
        client.gate.slot=Mock(return_value=nullcontext())
        lookups, posts = [], []
        states=iter(statuses)
        def open_request(request, **kwargs):
            if '/room/v1/Room/get_info' in request.full_url:
                lookups.append(request)
                data={'uid':11,'live_status':next(states)}
            else:
                posts.append(request)
                data={}
            return io.BytesIO(json.dumps({'code':0,'data':data}).encode())
        client.opener.open=Mock(side_effect=open_request)
        client.tasks=Mock(side_effect=lambda uid:{'task_info':[
            {'jump_type':'sendDanmu','sub_title':f'每日上限 {len(posts)}/10'}]})
        return client,lookups,posts

    def test_ten_messages_share_two_checks_including_outer_queue_loop(self):
        client,lookups,posts=self.grouped_client()
        with patch('index.time.monotonic',return_value=1000), patch('index.time.sleep'):
            cloud.free_actions(client,22,11,client.tasks(11),cloud.today(),1600,10)
        self.assertEqual(len(posts),10)
        self.assertEqual(len(lookups),2)

    def test_goes_live_at_second_group_stops_after_five_messages(self):
        client,lookups,posts=self.grouped_client((0,1))
        audit={}
        with patch('index.time.monotonic',return_value=1000), patch('index.time.sleep'):
            cloud.free_actions(client,22,11,client.tasks(11),cloud.today(),1600,10,audit)
        self.assertEqual(len(posts),5)
        self.assertEqual(len(lookups),2)
        self.assertEqual(audit['sendDanmu']['stop'],'not_offline')

    def test_long_wait_rechecks_even_with_unused_group_sends(self):
        client,lookups,posts=self.grouped_client((0,1))
        with patch('index.time.monotonic',return_value=1000):
            self.assertTrue(client.danmu(22,'test'))
        with patch('index.time.monotonic',return_value=1031):
            self.assertFalse(client.danmu(22,'test'))
        self.assertEqual(len(lookups),2)
        self.assertEqual(len(posts),1)

    def test_changing_rooms_never_reuses_previous_room_status(self):
        client,lookups,posts=self.grouped_client((0,0,1))
        with patch('index.time.monotonic',return_value=1000):
            self.assertTrue(client.danmu(22,'test'))
            self.assertTrue(client.danmu(33,'test'))
            self.assertFalse(client.danmu(22,'test'))
        self.assertEqual(len(lookups),3)
        self.assertEqual(len(posts),2)

    def test_live_sui_never_sends_even_with_ten_rounds_remaining(self):
        for uid, room in [(cloud.SUI_UID,cloud.SUI_ROOM),(11,22)]:
            for status in (1,2,None):
                client=Mock()
                client.gate=cloud.AccountGate()
                client.room.return_value={'uid':uid,'live_status':status}
                data={'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 0/10'}]}
                cloud.free_actions(client,room,uid,data,cloud.today(),time.monotonic()+60,10)
                client.danmu.assert_not_called()

    @patch('index.time.sleep')
    def test_offline_sui_still_gets_remaining_danmu(self, _):
        client=Mock()
        client.gate=cloud.AccountGate()
        client.room.return_value={'uid':cloud.SUI_UID,'live_status':0}
        client.tasks.return_value={'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 1/10'}]}
        data={'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 0/10'}]}
        cloud.free_actions(client,cloud.SUI_ROOM,cloud.SUI_UID,data,cloud.today(),time.monotonic()+60,1)
        client.danmu.assert_called_once()

    def test_room_goes_live_before_send_post_is_blocked(self):
        client=cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt='test-salt'
        client.room=Mock(return_value={'live_status':1})
        client.opener.open=Mock()
        self.assertFalse(client.danmu(cloud.SUI_ROOM,'test'))
        client.room.assert_called_once_with(cloud.SUI_ROOM, for_chat=True)
        client.opener.open.assert_not_called()

    def test_status_expires_while_waiting_for_request_slot_never_sends(self):
        client,lookups,posts=self.grouped_client((0,))
        with patch('index.time.monotonic', side_effect=[100.0, 131.0]):
            self.assertFalse(client.danmu(22,'test'))
        self.assertEqual(len(lookups),1)
        self.assertEqual(posts,[])

    def test_watcher_live_observation_invalidates_cached_check_before_post(self):
        from contextlib import contextmanager
        client,lookups,posts=self.grouped_client((0,1))
        @contextmanager
        def slot(endpoint):
            if endpoint == '/msg/send':
                client.room(22)
            yield
        client.gate.slot=slot
        with patch('index.time.monotonic',return_value=1000):
            self.assertFalse(client.danmu(22,'test'))
        self.assertEqual(len(lookups),2)
        self.assertEqual(posts,[])

    def test_boolean_status_is_unknown_and_never_sends(self):
        client=cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt='test-salt'
        client.room=Mock(return_value={'live_status':False})
        client.opener.open=Mock(return_value=io.BytesIO(b'{"code":0,"data":{}}'))
        self.assertFalse(client.danmu(22,'test'))
        client.opener.open.assert_not_called()

    def test_fresh_room_lookup_bypasses_cache(self):
        import json
        from urllib.parse import parse_qs, urlsplit
        client=cloud.Bili('SESSDATA=example; bili_jct=example',123)
        response=io.BytesIO(json.dumps({'code':0,'data':{'live_status':0}}).encode())
        client.opener.open=Mock(return_value=response)
        client.room(22, fresh=True)
        request=client.opener.open.call_args.args[0]
        query=parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query['room_id'], ['22'])
        self.assertIn('_ts', query)
        self.assertEqual(request.get_header('Cache-control'), 'no-cache, no-store')

    def test_status_lookup_failure_never_sends(self):
        client=cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.salt='test-salt'
        client.room=Mock(side_effect=cloud.TaskError('unavailable'))
        client.opener.open=Mock()
        with self.assertRaises(cloud.TaskError):
            client.danmu(cloud.SUI_ROOM,'test')
        client.opener.open.assert_not_called()

    def test_live_transition_skip_does_not_count_as_success_or_retry(self):
        client=Mock()
        client.gate=cloud.AccountGate()
        client.room.return_value={'uid':cloud.SUI_UID,'live_status':0}
        client.danmu.return_value=False
        data={'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 0/10'}]}
        cloud.free_actions(client,cloud.SUI_ROOM,cloud.SUI_UID,data,cloud.today(),time.monotonic()+60,10)
        client.danmu.assert_called_once()
        client.tasks.assert_not_called()
