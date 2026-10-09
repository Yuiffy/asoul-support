import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import index as cloud
from run_schedule import INTERVAL_SECONDS, MAX_RUN_SECONDS, reservation_slots
from test_cloud import Ledger


class RunScheduleTests(unittest.TestCase):
    def settings(self):
        return {'BILIBILI_ACCOUNTS_JSON': json.dumps([
            {'uid':123, 'role':'primary', 'cookie':'unused', 'other_medals':True},
            {'uid':456, 'role':'secondary', 'cookie':'disabled'}]),
            'ACTIVE_ACCOUNT_UIDS':'123', 'SUI_ONLY':'false', 'ENABLE_ACTIONS':'true'}

    def test_windows_include_manual_start_crossing_next_schedule(self):
        self.assertEqual(list(reservation_slots(0, 6900)), [0])
        self.assertEqual(list(reservation_slots(7199, 6900)), [0,1])
        self.assertEqual(list(reservation_slots(7200, 6900)), [1])
        self.assertEqual(list(reservation_slots(300, 6900)), [0])

    def test_overlapping_invocation_cannot_dispatch_account(self):
        ledger = Ledger()
        started, release = threading.Event(), threading.Event()
        def run(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError('Test invocation not released')
            return {'account':{'uid':123}}, []
        try:
            with patch('index.settings_from',return_value=self.settings()), \
                    patch('index.ObsLedger',return_value=ledger), patch('index.report',return_value=[]), \
                    patch('index.run_account_queue',side_effect=run) as queue, \
                    patch('index.time.time',side_effect=[7190,7200]), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                first = executor.submit(cloud.handler, {}, None)
                self.assertTrue(started.wait(5))
                second = cloud.handler({}, None)
                self.assertEqual(second['status'], 'duplicate_run')
                self.assertEqual(queue.call_count, 1)
                release.set()
                self.assertEqual(first.result(timeout=5)['status'],'finished')
        finally:
            release.set()

    def test_regular_two_hour_run_is_not_blocked_by_old_four_hour_key(self):
        ledger = Ledger()
        ledger.reserve('runs/queue-v2/0.json', {})
        with patch('index.settings_from',return_value=self.settings()), \
                patch('index.ObsLedger',return_value=ledger), patch('index.report',return_value=[]), \
                patch('index.run_account_queue',return_value=({'account':{'uid':123}},[])) as queue, \
                patch('index.time.time',side_effect=[0,INTERVAL_SECONDS]):
            self.assertEqual(cloud.handler({},None)['status'],'finished')
            self.assertEqual(cloud.handler({},None)['status'],'finished')
        self.assertEqual(queue.call_count, 2)
        self.assertTrue(queue.call_args.args[0]['other_medals'])
        self.assertEqual(queue.call_args.args[0]['uid'],123)

    def test_budget_is_bounded_and_short_cloud_timeout_makes_no_calls(self):
        with patch('index.settings_from',return_value=self.settings()), patch('index.Bili') as bili, \
                patch('index.ObsLedger') as ledger:
            with self.assertRaises(cloud.TaskError):
                cloud.handler({'max_seconds':MAX_RUN_SECONDS+1},None)
            context=Mock()
            context.getRemainingTimeInMilliSeconds.return_value=10000
            self.assertEqual(cloud.handler({},context)['status'],'insufficient_time')
        bili.assert_not_called()
        ledger.assert_not_called()

    def test_health_exposes_two_hour_schedule(self):
        with patch('index.settings_from',return_value=self.settings()):
            result=cloud.handler({'mode':'health'},None)
        self.assertEqual(result['schedule']['interval_seconds'],7200)
        self.assertEqual(result['schedule']['max_run_seconds'],6600)
        self.assertEqual(result['scope']['active_account_uids'],[123])
        self.assertFalse(result['scope']['sui_only'])
