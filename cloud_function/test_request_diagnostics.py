import io
import json
import unittest
import urllib.parse
from email.message import Message
from unittest.mock import Mock, patch

import index as cloud
from wecom_notify import events


class RequestDiagnosticTests(unittest.TestCase):
    def test_first_like_rejection_is_distinct_from_many_room_requests_without_secrets(self):
        gate = cloud.AccountGate(diagnostics=True)
        client = cloud.Bili('SESSDATA=private-session; bili_jct=private-csrf; '
                            'buvid3=private-device; bili_ticket=private-ticket', 123, gate)
        client.salt = 'test-salt'
        rejected = io.BytesIO(json.dumps({'code': -352, 'message': 'private-csrf',
                                          'data': {'v_voucher': 'private-challenge'}}).encode())
        rejected.headers = Message()
        rejected.headers['x-bili-gaia-vvoucher'] = 'private-header'
        client.opener.open = Mock(side_effect=[io.BytesIO(b'{"code":0,"data":{}}'), rejected])
        with patch('index.time.sleep'), patch('index.time.monotonic', return_value=1000), \
                patch('index.random.uniform', return_value=0):
            client.room(25788785)
            with self.assertRaises(cloud.ApiError):
                client.like(25788785, cloud.SUI_UID, 30)
            with self.assertRaises(cloud.TaskError):
                client.like(25788785, cloud.SUI_UID, 30)
        self.assertEqual(client.opener.open.call_count, 2)
        summary = gate.diagnostic_summary()
        self.assertEqual(sum(summary['request_counts'].values()), 2)
        self.assertEqual(summary['request_counts'][cloud.LIKE_ENDPOINT], 1)
        last = summary['last_rejection']
        self.assertTrue(last['challenge_present'])
        self.assertTrue(last['cookie_fields_present']['buvid3'])
        self.assertFalse(last['cookie_fields_present']['buvid4'])
        self.assertTrue(last['wbi_signed'])
        self.assertTrue(last['post_body_empty'])
        self.assertEqual(last['like_click_count'], 30)
        self.assertNotIn('private-', json.dumps(summary))
        # BLTH's contract: POST with signed query parameters and empty body.
        request = client.opener.open.call_args.args[0]
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.data, b'')
        self.assertEqual(params['click_time'], ['30'])
        self.assertEqual(params['csrf'], ['private-csrf'])
        self.assertIn('w_rid', params)

    def test_header_challenge_without_response_voucher_is_detected(self):
        gate = cloud.AccountGate(diagnostics=True)
        client = cloud.Bili('SESSDATA=example; bili_jct=example', 123, gate)
        rejected = io.BytesIO(b'{"code":-352,"data":null}')
        rejected.headers = Message()
        rejected.headers['x-bili-gaia-vvoucher'] = 'private-header'
        client.opener.open = Mock(return_value=rejected)
        with patch('index.time.sleep'), self.assertRaises(cloud.ApiError):
            client.room(1)
        self.assertTrue(gate.diagnostic_summary()['last_rejection']['challenge_present'])

    def test_disabled_diagnostics_retains_no_request_details(self):
        gate = cloud.AccountGate()
        with gate.slot('/room/v1/Room/get_info'):
            pass
        self.assertIsNone(gate.diagnostic_summary())
        self.assertEqual(gate.request_counts, {})

    def test_risk_report_includes_requests_and_refresh_state(self):
        identity = {'account': {'uid': 123}, 'paused_by_risk': True,
                    'credential_refresh': 'not_required', 'request_diagnostics': {
                        'version': 'like-risk-v1', 'request_counts': {cloud.LIKE_ENDPOINT: 1,
                            '/room/v1/Room/get_info': 2},
                        'last_rejection': {'previous_request_gap_seconds': 1.5,
                            'elapsed_seconds': 10, 'challenge_present': True,
                            'cookie_fields_present': {'buvid3': False, 'buvid4': False}}}}
        row = {'account': {'uid': 123}, 'room': 25788785, 'status': 'error'}
        text = events('2026-10-11', [identity], [row], 123, 1)[0][1]
        self.assertIn('请求尝试3次；点赞请求1次', text)
        self.assertIn('拒绝前请求间隔1.5秒', text)
        self.assertIn('验证码标记有', text)
        self.assertIn('buvid3无', text)
        self.assertIn('登录凭据维护：not_required', text)


if __name__ == '__main__':
    unittest.main()
