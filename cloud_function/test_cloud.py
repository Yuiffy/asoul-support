import copy
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import index as cloud
from asoul_x25kn import sign


def medal(current=0, done=False, price=100):
    return {'task_info': [{'jump_type': 'feedLight', 'title': '投喂粉丝灯牌',
                           'sub_title': f'每日上限 {current}/1', 'is_done': done}],
            'fans_club_gift_info': {'gift_id': 31164, 'price': price}}


class Ledger:
    def __init__(self):
        self.keys = set()
        self.lock = threading.Lock()

    def reserve(self, key, value):
        with self.lock:
            if key in self.keys:
                return False
            self.keys.add(key)
            return True


@patch('index.time.sleep')
class GiftTests(unittest.TestCase):
    def client(self, data=None):
        client = Mock(uid=123)
        client.watch_seconds = 0
        client.tasks.return_value = medal() if data is None else data
        return client

    def test_manual_gift_is_skipped(self, _):
        client = self.client(medal(1, True))
        self.assertEqual(cloud.maybe_gift(client, Ledger(), True, cloud.today()), 'already_done')
        client.gift.assert_not_called()

    def test_paid_disabled_makes_no_requests(self, _):
        client = self.client()
        self.assertEqual(cloud.maybe_gift(client, Ledger(), False, cloud.today()), 'disabled_for_account')
        client.tasks.assert_not_called()
        client.gift.assert_not_called()

    def test_price_increase_is_rejected(self, _):
        client = self.client(medal(price=101))
        self.assertEqual(cloud.maybe_gift(client, Ledger(), True, cloud.today()), 'unexpected_gift_or_price')
        client.gift.assert_not_called()

    def test_unknown_daily_state_is_rejected(self, _):
        client = self.client({'task_info': []})
        self.assertEqual(cloud.maybe_gift(client, Ledger(), True, cloud.today()), 'unknown_gift_task')
        client.gift.assert_not_called()

    def test_timeout_does_not_retry_even_in_new_client(self, _):
        ledger = Ledger()
        client = self.client()
        client.gift.side_effect = cloud.TaskError('uncertain')
        with self.assertRaises(cloud.TaskError):
            cloud.maybe_gift(client, ledger, True, cloud.today())
        fresh = self.client()
        self.assertEqual(cloud.maybe_gift(fresh, ledger, True, cloud.today()), 'already_reserved')
        fresh.gift.assert_not_called()

    def test_concurrent_calls_cannot_duplicate_gift(self, _):
        ledger, client = Ledger(), self.client()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: cloud.maybe_gift(client, ledger, True, cloud.today()), range(12)))
        client.gift.assert_called_once_with(31164, 100)

    def test_manual_gift_during_reservation(self, _):
        client = self.client()
        client.tasks.side_effect = [medal(), medal(1, True)]
        self.assertEqual(cloud.maybe_gift(client, Ledger(), True, cloud.today()), 'changed_after_reservation')
        client.gift.assert_not_called()

    def test_cross_day_never_sends(self, _):
        client = self.client()
        self.assertEqual(cloud.maybe_gift(client, Ledger(), True, '2000-01-01'), 'cross_day')
        client.gift.assert_not_called()

    def test_ledger_outage_prevents_payment(self, _):
        client, ledger = self.client(), Mock()
        ledger.reserve.side_effect = cloud.TaskError('ledger unavailable')
        with self.assertRaises(cloud.TaskError):
            cloud.maybe_gift(client, ledger, True, cloud.today())
        client.gift.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_expired_secondary_does_not_block_primary(self):
        accounts = [{'role': 'primary', 'uid': 123, 'cookie': 'one'},
                    {'role': 'secondary', 'uid': 456, 'cookie': 'two'}]
        good, bad = Mock(), Mock()
        good.login.return_value = {'uid': 123}
        good.tasks.return_value = medal()
        bad.login.side_effect = cloud.TaskError('expired')
        with patch.dict(os.environ, {'BILIBILI_ACCOUNTS_JSON': json.dumps(accounts)}), patch('index.Bili', side_effect=[good,bad]):
            result = cloud.handler({'mode': 'inspect'}, None)
        self.assertEqual(result['accounts'][0]['account']['uid'], 123)
        self.assertIn('tasks', result['accounts'][0])
        self.assertEqual(result['accounts'][1]['status'], 'error')

    def test_secondary_cannot_pay_even_if_misconfigured(self):
        account = {'role': 'secondary', 'uid': 123, 'cookie': 'unused', 'allow_paid': True}
        settings = {'PAID_ACCOUNT_UID': '123', 'ENABLE_PAID_GIFT': 'true'}
        client = Mock(uid=123)
        client.watch_seconds = 0
        data = medal()
        data['reach_free_intimacy_limit'] = True
        client.tasks.return_value = data
        client.login.return_value = {'uid': 123}
        with patch('index.Bili', return_value=client), patch('index.maybe_gift', return_value='disabled_for_account') as gift:
            cloud.run_room(account, cloud.SUI_ROOM, cloud.SUI_UID, cloud.today(), 0, Ledger(), settings)
        self.assertIs(gift.call_args.args[2], False)

    def test_functiongraph_context_decrypts_environment(self):
        context = Mock()
        context.getUserData.side_effect = lambda key: {'ENABLE_ACTIONS': 'true'}.get(key)
        with patch.dict(os.environ, {'ENABLE_ACTIONS': 'false'}):
            self.assertEqual(cloud.settings_from(context)['ENABLE_ACTIONS'], 'true')

    def test_upstream_hmac_vector(self):
        payload = ('{"platform":"web","parent_id":9,"area_id":371,"seq_id":1,'
                   '"room_id":22632424,"buvid":"TEST-BUVID",'
                   '"uuid":"00000000-0000-4000-8000-000000000000",'
                   '"ets":1700000000,"time":60,"ts":1700000060000}')
        self.assertEqual(sign(payload, [2,5,1,4], 'seacasdgyijfhofiuxoannn'),
                         '09159f943eb73570f2f6395b35291397f666444492e0c4e0197fcc7f509f850b'
                         '37a06aade2a8d2657e367e400aacc0817d5fc0ca900a1bf33e9b6beb6d8eb91d')

    def test_per_round_done_is_not_daily_completion(self):
        data = medal(0, True)
        data['task_info'][0].update(jump_type='like', sub_title='每日上限 3/10')
        self.assertTrue(cloud.pending(data, 'like'))

    def test_completed_daily_task_stops(self):
        data = medal(1, True)
        self.assertFalse(cloud.pending(data, 'feedLight'))

    def test_inspection_and_missing_credentials_never_execute(self):
        with patch.dict(os.environ, {'BILIBILI_ACCOUNTS_JSON': '[]', 'ENABLE_ACTIONS': 'true'}):
            self.assertEqual(cloud.handler({}, None)['status'], 'needs_credentials')

    def test_secret_is_not_in_transport_exception(self):
        client = cloud.Bili('SESSDATA=verysecret; bili_jct=csrf', 123)
        client.opener.open = Mock(side_effect=RuntimeError('verysecret'))
        with self.assertRaises(cloud.TaskError) as caught:
            client.login()
        self.assertNotIn('verysecret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
