import json
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

import index as cloud
from daily_progress import DailyProgress, ProgressError
from test_cloud import Ledger

DAY = '2026-09-15'
ACCOUNT = {'uid': 123, 'role': 'secondary', 'cookie': 'private', 'other_medals': True}
FULL = {k: [10, 10] for k in ('watchLive', 'sendDanmu', 'like')}


class DailyProgressTests(unittest.TestCase):
    def test_obs_missing_journal_is_distinct_from_missing_read_permission(self):
        context = Mock()
        context.getSecurityAccessKey.return_value = 'test-ak'
        context.getSecuritySecretKey.return_value = 'test-sk'
        context.getSecurityToken.return_value = 'test-token'
        ledger = cloud.ObsLedger(context, 'test-daily-bucket')
        key = 'runs/daily/123/day.jsonl'
        ledger.opener.open = Mock(side_effect=urllib.error.HTTPError('https://example.invalid',404,'missing',{},None))
        self.assertIsNone(ledger.read_progress(key))
        ledger.opener.open.side_effect = urllib.error.HTTPError('https://example.invalid',403,'denied',{},None)
        ledger.reserve = Mock(return_value=False)
        with self.assertRaisesRegex(ProgressError, '403'):
            ledger.read_progress(key)

    def test_missing_object_403_uses_create_only_marker_and_then_reads(self):
        ledger = cloud.ObsLedger(Mock(), 'test-daily-bucket')
        marker = b'{"type":"init","schema":1}\n'
        ledger._progress_request = Mock(side_effect=[ProgressError('OBS daily progress HTTP 403'), marker])
        ledger.reserve = Mock(return_value=True)
        self.assertEqual(ledger.read_progress('runs/daily/123/day.jsonl'), marker)
        ledger.reserve.assert_called_once_with('runs/daily/123/day.jsonl', {'type':'init','schema':1})

    def test_init_marker_keeps_correct_append_offset(self):
        ledger = Ledger()
        state = DailyProgress(ledger, ACCOUNT, {}, DAY)
        ledger.journals[state.key] = b'{"type":"init","schema":1}\n'
        state = self.create(ledger=ledger)
        state.record(1, {'after': FULL}, DAY)
        resumed = DailyProgress(ledger, ACCOUNT, {}, DAY)
        self.assertIn(1, resumed.done)

    def test_existing_journal_403_never_overwrites_it(self):
        ledger = cloud.ObsLedger(Mock(), 'test-daily-bucket')
        ledger._progress_request = Mock(side_effect=[ProgressError('OBS daily progress HTTP 403'), b'existing'])
        ledger.reserve = Mock(return_value=False)
        self.assertEqual(ledger.read_progress('runs/daily/123/day.jsonl'), b'existing')

    def create(self, ledger=None, account=None, settings=None, day=DAY):
        state = DailyProgress(ledger or Ledger(), account or ACCOUNT, settings or {}, day)
        if state.header is None:
            state.initialize({'uid': (account or ACCOUNT)['uid'], 'name': '测试'}, [(cloud.SUI_ROOM, cloud.SUI_UID), (1, 11), (2, 22)], 3)
        return state

    def test_cold_start_resumes_remaining_rooms(self):
        state = self.create()
        state.record(1, {'after': FULL}, DAY)
        resumed = DailyProgress(state.ledger, ACCOUNT, {}, DAY)
        self.assertEqual(resumed.remaining(), [(cloud.SUI_ROOM, cloud.SUI_UID), (2, 22)])

    def test_new_day_has_no_old_completion_or_roster(self):
        state = self.create()
        state.record(1, {'after': FULL}, DAY)
        tomorrow = DailyProgress(state.ledger, ACCOUNT, {}, '2026-09-16')
        self.assertIsNone(tomorrow.header)
        self.assertEqual(tomorrow.done, {})
        self.assertFalse(state.record(2, {'after': FULL}, '2026-09-16'))

    def test_pending_storage_error_and_unknown_tasks_are_not_completed(self):
        state = self.create()
        for row in ({'after': {}, 'storage_full': True}, {'after': FULL, 'status': 'error'},
                    {'after': {'watchLive': [2,10]}}):
            self.assertFalse(state.record(1, row, DAY))
        self.assertIn((1,11), DailyProgress(state.ledger, ACCOUNT, {}, DAY).remaining())

    def test_unvisited_tail_precedes_previous_partial_room(self):
        state = self.create()
        state.record(1, {'after': {'watchLive': [1,10]}}, DAY)
        self.assertEqual(state.remaining(), [(cloud.SUI_ROOM, cloud.SUI_UID), (2,22), (1,11)])

    def test_paid_primary_requires_lamp_completion(self):
        account = dict(ACCOUNT, role='primary', allow_paid=True)
        settings = {'PAID_ACCOUNT_UID':'123', 'ENABLE_PAID_GIFT':'true'}
        state = self.create(account=account, settings=settings)
        self.assertFalse(state.record(cloud.SUI_ROOM, {'after': dict(FULL,feedLight=[0,1])}, DAY))
        self.assertTrue(state.record(cloud.SUI_ROOM, {'after': dict(FULL,feedLight=[1,1])}, DAY))

    def test_cookie_change_keeps_progress_policy_change_does_not(self):
        state = self.create()
        state.record(1, {'after': FULL}, DAY)
        changed_cookie = DailyProgress(state.ledger, dict(ACCOUNT,cookie='new-private'), {}, DAY)
        self.assertIn(1, changed_cookie.done)
        changed_scope = DailyProgress(state.ledger, dict(ACCOUNT,banned_uids=[11]), {}, DAY)
        self.assertIsNone(changed_scope.header)
        self.assertNotIn(b'private', state.ledger.journals[state.key])

    def test_uncertain_append_is_reconciled_after_reload(self):
        state = self.create()
        original = state.ledger.append_progress
        def committed_then_timeout(key, data, position):
            original(key, data, position)
            raise TimeoutError()
        state.ledger.append_progress = committed_then_timeout
        with self.assertRaises(ProgressError):
            state.record(1, {'after': FULL}, DAY)
        self.assertNotIn(1, state.done)
        self.assertIn(1, DailyProgress(state.ledger, ACCOUNT, {}, DAY).done)

    def test_stale_writer_cannot_overwrite_a_new_checkpoint(self):
        first = self.create()
        stale = DailyProgress(first.ledger, ACCOUNT, {}, DAY)
        first.record(1, {'after': FULL}, DAY)
        with self.assertRaises(ProgressError):
            stale.record(2, {'after': FULL}, DAY)
        restored = DailyProgress(first.ledger, ACCOUNT, {}, DAY)
        self.assertIn(1, restored.done)
        self.assertNotIn(2, restored.done)

    def test_corrupt_journal_is_not_treated_as_empty(self):
        state = self.create()
        state.ledger.journals[state.key] += b'{broken'
        with self.assertRaises(ProgressError):
            DailyProgress(state.ledger, ACCOUNT, {}, DAY)

    def test_cached_complete_account_makes_no_bilibili_requests(self):
        state = self.create()
        for room, _ in list(state.remaining()):
            state.record(room, {'after': FULL}, DAY)
        with patch('index.Bili') as client:
            metadata, rows = cloud.run_account_queue(ACCOUNT, DAY, time.monotonic()+60, state.ledger, {})
        client.assert_not_called()
        self.assertEqual(rows, [])
        self.assertEqual(metadata['daily_completed_rooms'], 3)

    def test_second_queue_run_never_refetches_roster_or_completed_rooms(self):
        state = self.create()
        state.record(cloud.SUI_ROOM, {'after': FULL}, DAY)
        state.record(1, {'after': FULL}, DAY)
        client = Mock(uid=123,watch_seconds=0)
        client.gate = cloud.AccountGate()
        client.login.return_value={'uid':123,'name':'测试'}
        client.tasks.return_value={'task_info':[{'jump_type':k,'sub_title':'每日上限 10/10'} for k in FULL]}
        with patch('index.Bili', return_value=client), patch('index.today',return_value=DAY):
            metadata, rows = cloud.run_account_queue(ACCOUNT, DAY, time.monotonic()+60, state.ledger, {})
        client.medal_rooms.assert_not_called()
        client.tasks.assert_called_once_with(22)
        self.assertEqual([r['room'] for r in rows], [2])
        self.assertEqual(metadata['cached_completed_rooms'], 2)
        self.assertEqual(metadata['remaining_rooms'], 0)

    def test_completed_checkpoint_saved_before_later_room_failure(self):
        ledger = Ledger()
        client = Mock(uid=123,watch_seconds=0)
        client.gate = cloud.AccountGate()
        client.login.return_value={'uid':123,'name':'测试'}
        client.medal_rooms.return_value={cloud.SUI_ROOM:cloud.SUI_UID,1:11}
        data={'task_info':[{'jump_type':k,'sub_title':'每日上限 10/10'} for k in FULL]}
        client.tasks.side_effect=[data,data,cloud.TaskError('failure')]
        with patch('index.Bili',return_value=client), patch('index.today',return_value=DAY):
            metadata, rows=cloud.run_account_queue(ACCOUNT,DAY,time.monotonic()+60,ledger,{})
        resumed=DailyProgress(ledger,ACCOUNT,{},DAY)
        self.assertIn(cloud.SUI_ROOM,resumed.done)
        self.assertEqual(resumed.remaining(),[(1,11)])

    def test_checkpoint_failure_stops_further_rooms_and_is_not_reported_as_bili_risk(self):
        state = self.create()
        state.ledger.append_progress = Mock(side_effect=TimeoutError())
        client = Mock(uid=123, watch_seconds=0)
        client.gate = cloud.AccountGate()
        client.login.return_value = {'uid':123, 'name':'测试'}
        client.tasks.return_value = {'task_info':[{'jump_type':k,'sub_title':'每日上限 10/10'} for k in FULL]}
        with patch('index.Bili',return_value=client), patch('index.today',return_value=DAY):
            metadata, rows = cloud.run_account_queue(ACCOUNT,DAY,time.monotonic()+60,state.ledger,{})
        self.assertEqual(len(rows),1)
        self.assertEqual(metadata['status'],'error')
        self.assertFalse(metadata['paused_by_risk'])
        self.assertEqual({call.args[0] for call in client.tasks.call_args_list}, {cloud.SUI_UID})


if __name__ == '__main__':
    unittest.main()
