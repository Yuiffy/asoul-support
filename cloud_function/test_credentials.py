import io
import json
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import index as cloud


class CredentialStatusTests(unittest.TestCase):
    def test_qr_login_preserves_refresh_token_locally_without_printing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'qr-state.json').write_text(json.dumps({'qrcode_key': 'test-key'}), encoding='utf-8')
            response = {'code': 0, 'data': {'code': 0, 'refresh_token': 'private-refresh',
                'url': 'https://www.bilibili.com/?SESSDATA=private-session&bili_jct=private-csrf&DedeUserID=123'}}
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(response).encode())
            stdout = io.StringIO()
            with patch('sys.argv', ['qr_login.py', 'poll', directory]), \
                 patch('urllib.request.build_opener', return_value=opener), redirect_stdout(stdout):
                runpy.run_path(str(Path(__file__).with_name('qr_login.py')))
            stored = json.loads((root / 'accounts.local.json').read_text(encoding='utf-8'))
            self.assertEqual(stored['uid'], 123)
            self.assertEqual(stored['refresh_token'], 'private-refresh')
            self.assertNotIn('private-', stdout.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())['refresh_token_present'])

    def settings(self):
        return {'ACTIVE_ACCOUNT_UIDS': '123', 'ENABLE_ACTIONS': 'true',
                'BILIBILI_ACCOUNTS_JSON': json.dumps([
                    {'uid': 123, 'role': 'primary', 'cookie':
                     'SESSDATA=private-session; bili_jct=private-csrf; buvid3=private-device',
                     'ac_time_value': 'private-refresh'},
                    {'uid': 456, 'role': 'secondary', 'cookie': 'disabled-secret'}])}

    def client(self, refresh):
        client = cloud.Bili('SESSDATA=private-session; bili_jct=private-csrf; buvid3=private-device', 123)
        nav = {'code': 0, 'data': {'isLogin': True, 'mid': 123, 'uname': 'name',
                'wbi_img': {'img_url': 'https://i.test/' + 'a' * 32 + '.png',
                            'sub_url': 'https://i.test/' + 'b' * 32 + '.png'}}}
        client.opener.open = Mock(side_effect=[io.BytesIO(json.dumps(nav).encode()),
            io.BytesIO(json.dumps({'code': 0, 'data': {'refresh': refresh}}).encode())])
        return client

    @patch('index.time.sleep')
    def test_only_selected_account_and_read_only_gets_with_no_secret_output(self, _):
        for refresh in (True, False):
            with self.subTest(refresh=refresh):
                client = self.client(refresh)
                with patch('index.settings_from', return_value=self.settings()), \
                     patch('index.Bili', return_value=client) as factory, \
                     patch('index.ObsLedger') as ledger, patch('index.report') as report:
                    result = cloud.handler({'mode': 'credential_status'}, None)
                factory.assert_called_once()
                self.assertEqual(result['accounts'][0]['logged_in'], True)
                self.assertEqual(result['accounts'][0]['refresh_required'], refresh)
                self.assertTrue(result['accounts'][0]['refresh_token_present'])
                self.assertFalse(result['accounts'][0]['automatic_refresh'])
                self.assertNotIn('private-', json.dumps(result))
                self.assertNotIn('disabled-secret', json.dumps(result))
                sent = [call.args[0] for call in client.opener.open.call_args_list]
                self.assertEqual([r.get_method() for r in sent], ['GET', 'GET'])
                self.assertTrue(sent[1].full_url.startswith(cloud.COOKIE_INFO + '?'))
                self.assertTrue(all(r.data is None for r in sent))
                ledger.assert_not_called()
                report.assert_not_called()

    def test_expired_login_does_not_query_refresh_or_claim_other_error_is_expiry(self):
        for code, logged_in in ((-101, False), (-352, None)):
            with self.subTest(code=code):
                client = self.client(False)
                client.opener.open = Mock(return_value=io.BytesIO(json.dumps(
                    {'code': code, 'message': 'private-session'}).encode()))
                with patch('index.settings_from', return_value=self.settings()), patch('index.Bili', return_value=client):
                    result = cloud.handler({'mode': 'credential_status'}, None)
                row = result['accounts'][0]
                self.assertIs(row['logged_in'], logged_in)
                self.assertIsNone(row['refresh_required'])
                self.assertNotIn('private-session', json.dumps(result))
                self.assertEqual(client.opener.open.call_count, 1)

    @patch('index.time.sleep')
    def test_missing_or_nonboolean_refresh_flag_is_unknown(self, _):
        client = self.client(1)
        with patch('index.settings_from', return_value=self.settings()), patch('index.Bili', return_value=client):
            row = cloud.handler({'mode': 'credential_status'}, None)['accounts'][0]
        self.assertIs(row['logged_in'], True)
        self.assertIsNone(row['refresh_required'])
        self.assertEqual(row['status'], 'error')

    def test_passport_allowlist_permits_only_cookie_info_get(self):
        client = self.client(False)
        for url, method in ((cloud.COOKIE_INFO, 'POST'),
                            ('https://passport.bilibili.com/x/passport-login/web/cookie/refresh', 'GET'),
                            ('https://passport.bilibili.com.evil.test/x/passport-login/web/cookie/info', 'GET')):
            with self.subTest(url=url, method=method), self.assertRaises(cloud.TaskError):
                client.request(url, method=method)
        client.opener.open.assert_not_called()
