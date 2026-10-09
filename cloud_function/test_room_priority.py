import io
import time
import unittest
import urllib.parse
from unittest.mock import Mock, patch

import index as cloud
from room_priority import prioritize
from test_cloud import Ledger


class RoomPriorityTests(unittest.TestCase):
    def test_stable_sui_live_offline_unknown_order(self):
        targets = [(1,11),(2,22),(3,33),(cloud.SUI_ROOM,cloud.SUI_UID),(4,44),(5,55)]
        self.assertEqual(prioritize(targets,{1:0,2:1,3:2,4:1}),
                         [targets[3],targets[1],targets[4],targets[0],targets[2],targets[5]])

    def test_live_late_in_roster_gets_all_like_rounds_before_offline(self):
        client=Mock(uid=123,watch_seconds=0)
        client.login.return_value={'uid':123}
        client.medal_rooms.return_value={1:11,2:22,cloud.SUI_ROOM:cloud.SUI_UID}
        client.live_statuses.return_value={1:0,2:1}
        data={'task_info':[{'jump_type':'like','sub_title':'每日上限 0/10'}]}
        done={'task_info':[{'jump_type':'like','sub_title':'每日上限 10/10'}]}
        client.tasks.return_value=data
        sequence=[]
        def free(*args):
            sequence.append((args[1],args[6]))
            return done
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='already_done'),patch('index.free_actions',side_effect=free):
            meta,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},cloud.today(),time.monotonic()+60,Ledger(),{})
        self.assertNotIn('status',meta)
        self.assertEqual(sequence,[(cloud.SUI_ROOM,10),(2,10),(1,1)])
        self.assertEqual([r['room'] for r in rows],[cloud.SUI_ROOM,2,1])

    def test_batch_get_uses_repeated_uids_and_checks_room_identity(self):
        client=cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.opener.open=Mock(return_value=io.BytesIO(b'{"code":0,"data":{"11":{"uid":11,"room_id":1,"live_status":1},"22":{"uid":22,"room_id":99,"live_status":0}}}'))
        statuses=client.live_statuses([(1,11),(2,22)],cloud.today(),time.monotonic()+60)
        request=client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(),'GET')
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query),{'uids[]':['11','22']})
        self.assertEqual(statuses,{1:1})

    @patch('index.time.sleep')
    def test_room_now_live_never_sends_offline_danmaku(self,_):
        client=Mock()
        client.gate=cloud.AccountGate()
        client.room.return_value={'uid':11,'live_status':1}
        data={'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 0/10'}]}
        cloud.free_actions(client,1,11,data,cloud.today(),time.monotonic()+60,10)
        client.danmu.assert_not_called()
