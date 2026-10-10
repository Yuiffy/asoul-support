import json
import unittest
from unittest.mock import Mock, patch

import index as cloud


class LikeProbeTests(unittest.TestCase):
    def run_probe(self, *, reserved=True, enabled=True, error=None):
        settings = {'ENABLE_ACTIONS': 'true' if enabled else 'false'}
        account = {'uid': 123, 'role': 'primary', 'cookie': 'private-cookie'}
        client = Mock()
        client.room.return_value = {'uid': cloud.SUI_UID, 'live_status': 1}
        client.cookie_status.return_value = {'refresh_required': False}
        client.tasks.return_value = {'task_info': []}
        client.like.side_effect = error
        ledger = Mock()
        ledger.reserve.return_value = reserved
        with patch('index.settings_from', return_value=settings), \
                patch('index.active_accounts', return_value=[account]), \
                patch('index.ObsLedger', return_value=ledger), \
                patch('index.credential_account', return_value=(account, 'ready')) as credentials, \
                patch('index.Bili', return_value=client), \
                patch('index.run_account_queue') as queue, patch('index.report') as report:
            result = cloud.handler({'mode': 'like_probe', 'probe_id': 'a' * 32}, None)
        queue.assert_not_called()
        report.assert_not_called()
        client.danmu.assert_not_called()
        client.trace.assert_not_called()
        self.assertNotIn('private-', json.dumps(result))
        return result, client, credentials

    def test_exactly_one_fixed_primary_sui_like_and_read_only_credentials(self):
        result, client, credentials = self.run_probe()
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['code'], 0)
        client.like.assert_called_once_with(cloud.SUI_ROOM, cloud.SUI_UID, 30)
        self.assertTrue(credentials.call_args.kwargs['read_only'])

    def test_rejection_returns_code_without_retry(self):
        result, client, _ = self.run_probe(error=cloud.ApiError(-352, cloud.LIKE_ENDPOINT, 'secret'))
        self.assertEqual(result['code'], -352)
        self.assertEqual(result['endpoint'], cloud.LIKE_ENDPOINT)
        self.assertEqual(result['status'], 'rejected')
        client.like.assert_called_once()

    def test_duplicate_and_disabled_probes_do_not_call_bilibili(self):
        for options, status in (({'reserved': False}, 'duplicate_probe'),
                                ({'enabled': False}, 'actions_disabled')):
            result, client, credentials = self.run_probe(**options)
            self.assertEqual(result['status'], status)
            self.assertEqual(client.mock_calls, [])
            credentials.assert_not_called()

    def test_missing_probe_id_fails_before_io(self):
        with patch('index.settings_from', return_value={}), patch('index.ObsLedger') as ledger:
            with self.assertRaises(cloud.TaskError):
                cloud.handler({'mode': 'like_probe'}, None)
        ledger.assert_not_called()

    def test_optional_console_entry_maps_health_to_same_daily_probe_id(self):
        with patch('index.today', return_value='2026-10-11'), patch('index.handler') as handler:
            cloud.manual_like_probe_handler({'mode': 'health'}, None)
            first = handler.call_args.args[0]
            cloud.manual_like_probe_handler({'mode': 'health'}, None)
            self.assertEqual(first, handler.call_args.args[0])
            self.assertEqual(first['mode'], 'like_probe')
            self.assertEqual(len(first['probe_id']), 32)

    def test_optional_console_entry_delegates_timer_without_modification(self):
        event, context = {'trigger_type': 'TIMER'}, object()
        with patch('index.handler', return_value={'status': 'finished'}) as handler:
            self.assertEqual(cloud.manual_like_probe_handler(event, context), {'status': 'finished'})
        handler.assert_called_once_with(event, context)


if __name__ == '__main__':
    unittest.main()
