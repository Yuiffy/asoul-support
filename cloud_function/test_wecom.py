import io
import unittest
from unittest.mock import Mock, patch
from wecom_notify import events, send_notice


class WeComTests(unittest.TestCase):
    def test_changed_robot_uses_separate_deduplication_record(self):
        ledger, opener = Mock(), Mock()
        opener.open.side_effect = [io.BytesIO(b'{"errcode":0}'), io.BytesIO(b'{"errcode":0}')]
        with patch('wecom_notify.urllib.request.build_opener',return_value=opener):
            for key in ('first','second'):
                send_notice({'WECOM_WEBHOOK_URL':'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='+key},ledger,'day-test','text')
        self.assertNotEqual(ledger.reserve.call_args_list[0].args[0], ledger.reserve.call_args_list[1].args[0])

    def test_partial_progress_is_not_counted_as_daily_complete(self):
        row = {'room': 25788785, 'account': {'uid': 123}, 'before': {'watchLive':[2,10]}, 'after': {'watchLive':[3,10]}}
        message = events('2026-09-14', [], [row], 123, 1)[0][1]
        self.assertIn('免费日任务已满0间', message)
        self.assertIn('本轮有进展1间', message)

    def test_completed_primary_includes_verified_lamp(self):
        row = {'room':25788785,'account':{'uid':123},'after':{'watchLive':[10,10],'like':[10,10],'sendDanmu':[10,10],'feedLight':[0,1]}}
        self.assertIn('灯牌0/1', events('2026-09-14', [], [row], 123)[0][1])
        row['after']['feedLight'] = [1,1]
        self.assertEqual(len(events('2026-09-14', [], [row], 123)), 1)

    def test_aggregate_counts_all_rooms_and_separates_accounts(self):
        full = {'watchLive':[10,10], 'sendDanmu':[10,10], 'like':[10,10]}
        identities = [{'account':{'uid':123,'name':'primary'},'medal_rooms_total':20}, {'account':{'uid':456,'name':'secondary'}}]
        rows = [{'room':1,'account':{'uid':123},'after':full},
                {'room':2,'account':{'uid':123},'storage_full':True,'after':{}},
                {'room':3,'account':{'uid':123},'status':'error','reason':'timeout'},
                {'room':1,'account':{'uid':456},'status':'duplicate_slot'}]
        notices=events('2026-09-14',identities,rows,123,12)
        self.assertEqual(len(notices),1)
        text=notices[0][1]
        self.assertIn('本轮检查：4次，涉及3个直播间',text)
        self.assertIn('primary：本轮覆盖3/20',text)
        self.assertIn('免费日任务已满1间',text)
        self.assertIn('储蓄满跳过1间；重复跳过0间；异常1间',text)
        self.assertIn('secondary：本轮覆盖1个',text)

    def test_duplicate_reservation_never_sends(self):
        ledger = Mock()
        ledger.reserve.return_value = False
        with patch('wecom_notify.urllib.request.build_opener') as opener:
            status = send_notice({'WECOM_WEBHOOK_URL':'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=example'}, ledger, 'day-complete', 'test')
        self.assertEqual(status, 'already_attempted')
        opener.assert_not_called()

    def test_webhook_host_must_be_wecom(self):
        ledger=Mock()
        self.assertEqual(send_notice({'WECOM_WEBHOOK_URL':'https://example.com/send?key=example'},ledger,'x','text'),'invalid_webhook')
        ledger.reserve.assert_not_called()

    def test_success_response_required(self):
        ledger, opener = Mock(), Mock()
        opener.open.return_value = io.BytesIO(b'{"errcode":0}')
        with patch('wecom_notify.urllib.request.build_opener',return_value=opener):
            self.assertEqual(send_notice({'WECOM_WEBHOOK_URL':'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=example'},ledger,'x','text'),'sent')
