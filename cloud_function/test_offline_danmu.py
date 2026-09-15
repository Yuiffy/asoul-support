import io
import time
import unittest
from unittest.mock import Mock, patch
import index as cloud


class OfflineDanmuTests(unittest.TestCase):
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
        client.room.assert_called_once_with(cloud.SUI_ROOM)
        client.opener.open.assert_not_called()

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
