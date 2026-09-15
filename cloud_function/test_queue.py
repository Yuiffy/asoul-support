import io
import json
import time
import unittest
from unittest.mock import Mock, patch
import index as cloud
from test_cloud import Ledger


class QueueTests(unittest.TestCase):
    def test_risk_rejection_stops_all_later_account_requests(self):
        gate = cloud.AccountGate()
        client = cloud.Bili('SESSDATA=secret1234; bili_jct=csrf1234',123,gate)
        client.opener.open = Mock(return_value=io.BytesIO(json.dumps({'code':-352,'message':'risk secret1234'}).encode()))
        with self.assertRaises(cloud.ApiError) as err:
            client.room(10)
        self.assertIn('/room/v1/Room/get_info',str(err.exception))
        self.assertNotIn('secret1234',str(err.exception))
        with self.assertRaises(cloud.TaskError):
            client.tasks(20)
        self.assertEqual(client.opener.open.call_count,1)

    def test_10030_disables_only_failing_endpoint_without_retry(self):
        gate=cloud.AccountGate()
        gate.rejected(10030,'/msg/send')
        self.assertFalse(gate.stopped)
        gate.check('/room/v1/Room/get_info')
        with self.assertRaises(cloud.TaskError):
            gate.check('/msg/send')

    def test_entire_199_room_list_is_visited_without_five_room_slice(self):
        client=Mock(uid=123,watch_seconds=0)
        client.login.return_value={'uid':123,'name':'primary'}
        rooms={cloud.SUI_ROOM:cloud.SUI_UID,**{r:1000+r for r in range(1,199)}}
        client.medal_rooms.return_value=rooms
        client.tasks.return_value={'task_info':[],'reach_free_intimacy_limit':True}
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='disabled_for_account'):
            meta,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},cloud.today(),time.monotonic()+60,Ledger(),{})
        self.assertEqual(len(rows),199)
        self.assertEqual(rows[0]['room'],cloud.SUI_ROOM)
        self.assertEqual(meta['pending_rooms'],0)

    def test_error_does_not_discard_known_primary_progress(self):
        client=Mock(uid=123,watch_seconds=0)
        client.login.return_value={'uid':123}
        data={'task_info':[{'jump_type':'watchLive','sub_title':'每日上限 3/10'}]}
        client.tasks.return_value=data
        account={'uid':123,'cookie':'unused'}
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='already_done'),patch('index.free_actions',side_effect=cloud.ApiError(10030,'/msg/send','rejected')):
            meta,rows=cloud.run_account_queue(account,cloud.today(),time.monotonic()+60,Ledger(),{})
        self.assertEqual(rows[0]['after']['watchLive'],[3,10])
        self.assertIn('/msg/send',rows[0]['reason'])

    def test_unprocessed_rooms_are_reported_on_risk_stop(self):
        gate=cloud.AccountGate()
        client=Mock(uid=123,watch_seconds=0)
        client.login.return_value={'uid':123}
        client.medal_rooms.return_value={cloud.SUI_ROOM:cloud.SUI_UID,1:11,2:22}
        def fail(uid):
            gate.rejected(-352,'/tasks')
            raise cloud.ApiError(-352,'/tasks','risk')
        client.tasks.side_effect=fail
        with patch('index.AccountGate',return_value=gate),patch('index.Bili',return_value=client):
            meta,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},cloud.today(),time.monotonic()+60,Ledger(),{})
        self.assertEqual(len(rows),1)
        self.assertEqual(meta['pending_rooms'],2)
        self.assertTrue(meta['paused_by_risk'])
