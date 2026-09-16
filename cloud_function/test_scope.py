import json
import time
import unittest
from unittest.mock import Mock, patch

import index as cloud
from daily_progress import DailyProgress
from test_cloud import Ledger
from wecom_notify import events


class ScopeTests(unittest.TestCase):
    def settings(self):
        return {'BILIBILI_ACCOUNTS_JSON': json.dumps([
            {'uid': 123, 'role': 'primary', 'cookie': 'primary-private', 'other_medals': True},
            {'uid': 456, 'role': 'secondary', 'cookie': 'secondary-private', 'other_medals': True}]),
            'ACTIVE_ACCOUNT_UIDS': '123', 'SUI_ONLY': 'true', 'ENABLE_ACTIONS': 'true'}

    def test_filter_preserves_credentials_and_does_not_edit_config(self):
        settings = self.settings()
        original = settings['BILIBILI_ACCOUNTS_JSON']
        accounts = cloud.active_accounts(settings)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]['uid'], 123)
        self.assertFalse(accounts[0]['other_medals'])
        self.assertEqual(accounts[0]['cookie'], 'primary-private')
        self.assertEqual(settings['BILIBILI_ACCOUNTS_JSON'], original)

    def test_invalid_or_unknown_override_fails_closed(self):
        for override in ({'ACTIVE_ACCOUNT_UIDS': '999'}, {'ACTIVE_ACCOUNT_UIDS': '123,'},
                         {'ACTIVE_ACCOUNT_UIDS': 'oops'}, {'SUI_ONLY': 'tru'}):
            with self.subTest(override=override), self.assertRaises(cloud.TaskError):
                cloud.active_accounts(dict(self.settings(), **override))

    def test_health_reports_scope_without_api_or_state_access(self):
        with patch('index.settings_from', return_value=self.settings()), patch('index.Bili') as bili, \
                patch('index.ObsLedger') as ledger:
            result = cloud.handler({'mode': 'health'}, None)
        self.assertEqual(result['scope']['active_account_uids'], [123])
        self.assertTrue(result['scope']['sui_only'])
        self.assertNotIn('private', json.dumps(result))
        bili.assert_not_called()
        ledger.assert_not_called()

    def test_inspection_never_constructs_disabled_account(self):
        client = Mock()
        client.login.return_value = {'uid': 123}
        client.tasks.return_value = {'task_info': []}
        with patch('index.settings_from', return_value=self.settings()), patch('index.Bili', return_value=client) as bili:
            result = cloud.handler({'mode': 'inspect'}, None)
        bili.assert_called_once_with('primary-private', 123)
        self.assertEqual(len(result['accounts']), 1)

    def test_normal_handler_dispatches_only_selected_account(self):
        with patch('index.settings_from', return_value=self.settings()), \
                patch('index.ObsLedger', return_value=Ledger()), \
                patch('index.run_account_queue', return_value=({'account': {'uid':123}}, [])) as queue, \
                patch('index.report', return_value=[]):
            cloud.handler({}, None)
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(queue.call_args.args[0]['uid'], 123)
        self.assertFalse(queue.call_args.args[0]['other_medals'])

    def test_progress_state_never_loads_disabled_account(self):
        state = Mock(header=None, done={})
        with patch('index.settings_from', return_value=self.settings()), patch('index.ObsLedger'), \
                patch('index.DailyProgress', return_value=state) as progress:
            result = cloud.handler({'mode': 'progress_state'}, None)
        self.assertEqual(progress.call_count, 1)
        self.assertEqual([a['uid'] for a in result['accounts']], [123])

    def test_sui_only_does_not_resume_old_roster_or_enumerate_medals(self):
        settings, ledger = self.settings(), Ledger()
        original = json.loads(settings['BILIBILI_ACCOUNTS_JSON'])[0]
        old = DailyProgress(ledger, original, settings, cloud.today())
        old.initialize({'uid': 123}, [(cloud.SUI_ROOM,cloud.SUI_UID), (99,999)], 2)
        client = Mock(uid=123, watch_seconds=0)
        client.login.return_value = {'uid':123}
        client.tasks.return_value = {'task_info':[], 'reach_free_intimacy_limit':True}
        with patch('index.Bili', return_value=client), patch('index.maybe_gift', return_value='already_done'):
            meta, rows = cloud.run_account_queue(cloud.active_accounts(settings)[0], cloud.today(),
                                                time.monotonic()+60, ledger, settings)
        client.medal_rooms.assert_not_called()
        self.assertEqual([r['room'] for r in rows], [cloud.SUI_ROOM])
        self.assertEqual(meta['medal_rooms_total'], 1)
        self.assertTrue(all(c.args == (cloud.SUI_UID,) for c in client.tasks.call_args_list))

    def test_notice_describes_single_account_scope(self):
        result = events('2026-09-16', [{'account':{'uid':123}}],
                        [{'account':{'uid':123}, 'room':cloud.SUI_ROOM, 'after':{}}], 123, sui_only=True)
        self.assertIn('1个账号分别统计', result[0][1])
        self.assertIn('其他主播任务已关闭', result[0][1])
        self.assertNotIn('首次建立全量列表', result[0][1])
