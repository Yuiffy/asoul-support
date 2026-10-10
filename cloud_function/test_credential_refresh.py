import io
import json
import unittest
from email.message import Message
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

import credential_refresh as cr
import index as cloud

OLD = 'SESSDATA=private-old; bili_jct=old-csrf; DedeUserID=123; buvid3=private-device'
NEW = 'SESSDATA=private-new; bili_jct=new-csrf; DedeUserID=123; buvid3=private-device'
ACCOUNT = {'uid': 123, 'cookie': OLD, 'refresh_token': 'private-token'}
DAY = '2026-10-10'


class Ledger:
    def __init__(self):
        self.records = {}
        self.calls = []
        self.fail = False

    def read_progress(self, key):
        return self.records.get(key)

    _progress_request = read_progress

    def append_progress(self, key, data, position):
        self.calls.append(data)
        if self.fail or len(self.records.get(key, b'')) != position:
            raise RuntimeError('private-storage-secret')
        self.records[key] = self.records.get(key, b'') + data


class RefreshLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.key = Fernet.generate_key().decode()
        self.journal = cr.EncryptedJournal(self.ledger, ACCOUNT, self.key)
        self.transport = Mock()
        self.transport.info.return_value = {'refresh': True, 'timestamp': 1234567890123}
        self.transport.rotate.side_effect = lambda state, _: dict(state, cookie=NEW,
            refresh_token='private-new-token', old_refresh_token=state['refresh_token'], phase='pending_confirm')
        self.verify = Mock()

    def run_refresh(self):
        return cr.maintain(self.journal, self.transport, DAY, self.verify)

    def reload(self, read_only=False, key=None, account=None):
        return cr.EncryptedJournal(self.ledger, account or ACCOUNT, key or self.key, read_only)

    def test_rotate_saves_new_credential_before_confirm_and_keeps_old_token(self):
        def confirm(state):
            persisted = self.reload().state
            self.assertEqual(persisted, state)
            self.assertEqual(state['cookie'], NEW)
            self.assertEqual(state['old_refresh_token'], 'private-token')
        self.transport.confirm.side_effect = confirm
        state, status = self.run_refresh()
        self.assertEqual(status, 'refreshed')
        self.assertEqual(state['phase'], 'ready')
        self.assertNotIn('old_refresh_token', state)
        self.assertEqual(self.verify.call_args_list[0].args, (OLD,))
        self.assertEqual(self.verify.call_args_list[1].args, (NEW,))
        self.assertNotIn(b'private-', b''.join(self.ledger.calls))
        self.assertNotIn(b'new-csrf', b''.join(self.ledger.calls))

    def test_check_only_once_per_day_across_cold_starts(self):
        self.run_refresh()
        self.journal = self.reload()
        self.transport.reset_mock()
        self.verify.reset_mock()
        self.assertEqual(self.run_refresh()[1], 'checked_today')
        self.transport.info.assert_not_called()
        self.verify.assert_not_called()
        cr.maintain(self.journal, self.transport, '2026-10-11', self.verify)
        self.transport.info.assert_called_once_with(NEW)

    def test_no_rotation_if_provider_says_no_refresh(self):
        self.transport.info.return_value = {'refresh': False}
        self.assertEqual(self.run_refresh()[1], 'not_required')
        self.transport.rotate.assert_not_called()
        self.transport.confirm.assert_not_called()
        self.assertEqual(self.reload().state['checked_day'], DAY)

    def test_missing_token_and_malformed_recommendation_do_not_rotate(self):
        for info, token in (({'refresh': True, 'timestamp': 1}, ''),
                            ({'refresh': 1}, 'private-token'),
                            ({'refresh': True, 'timestamp': '1'}, 'private-token')):
            with self.subTest(info=info):
                self.journal.state['refresh_token'] = token
                self.transport.info.return_value = info
                with self.assertRaises(cr.CredentialError):
                    self.run_refresh()
        self.transport.rotate.assert_not_called()

    def test_storage_failure_before_rotation_stops_without_post(self):
        self.ledger.fail = True
        with self.assertRaisesRegex(cr.CredentialError, 'persistence uncertain'):
            self.run_refresh()
        self.transport.rotate.assert_not_called()

    def test_unknown_post_outcome_is_fenced_across_runs(self):
        self.transport.rotate.side_effect = cr.CredentialError('outcome uncertain')
        with self.assertRaises(cr.CredentialError):
            self.run_refresh()
        self.journal = self.reload()
        self.assertEqual(self.journal.state['phase'], 'refresh_started')
        with self.assertRaisesRegex(cr.CredentialError, 'reimport'):
            self.run_refresh()
        self.assertEqual(self.transport.rotate.call_count, 1)

    def test_failed_save_after_post_does_not_confirm_or_retry_rotation(self):
        original = self.transport.rotate.side_effect
        def rotate(state, ts):
            self.ledger.fail = True
            return original(state, ts)
        self.transport.rotate.side_effect = rotate
        with self.assertRaises(cr.CredentialError):
            self.run_refresh()
        self.transport.confirm.assert_not_called()
        self.ledger.fail = False
        self.journal = self.reload()
        with self.assertRaises(cr.CredentialError):
            self.run_refresh()
        self.assertEqual(self.transport.rotate.call_count, 1)

    def test_pending_confirmation_resumes_with_new_cookie_without_rotating(self):
        self.transport.confirm.side_effect = cr.CredentialError('confirmation uncertain')
        with self.assertRaises(cr.CredentialError):
            self.run_refresh()
        self.journal = self.reload()
        self.assertEqual(self.journal.state['phase'], 'pending_confirm')
        self.transport.reset_mock()
        self.transport.confirm.side_effect = None
        self.verify.reset_mock()
        self.assertEqual(self.run_refresh()[1], 'refreshed')
        self.transport.rotate.assert_not_called()
        self.transport.info.assert_not_called()
        self.verify.assert_called_once_with(NEW)

    def test_failed_identity_verification_never_confirms(self):
        self.verify.side_effect = [None, cr.CredentialError('UID mismatch')]
        with self.assertRaises(cr.CredentialError):
            self.run_refresh()
        self.transport.confirm.assert_not_called()
        self.assertEqual(self.reload().state['phase'], 'pending_confirm')

    def test_encryption_tamper_wrong_key_and_truncated_journal_fail_closed(self):
        self.run_refresh()
        raw = self.ledger.records[self.journal.key]
        with self.assertRaises(cr.CredentialError):
            self.reload(key=Fernet.generate_key().decode())
        self.ledger.records[self.journal.key] = raw[:-1]
        with self.assertRaises(cr.CredentialError):
            self.reload()
        rows = [json.loads(line) for line in raw.splitlines()]
        rows[-1]['encrypted'] = rows[-1]['encrypted'][:25] + 'AAAA'
        self.ledger.records[self.journal.key] = b'\n'.join(json.dumps(r).encode() for r in rows) + b'\n'
        with self.assertRaises(cr.CredentialError):
            self.reload()

    def test_stale_writer_conflicts_before_rotation(self):
        stale = self.reload()
        self.journal.save(dict(self.journal.state, checked_day='2026-10-09'))
        with self.assertRaises(cr.CredentialError):
            cr.maintain(stale, self.transport, DAY, self.verify)
        self.transport.rotate.assert_not_called()

    def test_read_only_diagnostic_does_not_write_or_rotate(self):
        self.journal = self.reload(read_only=True)
        with self.assertRaises(cr.CredentialError):
            self.run_refresh()
        self.assertEqual(self.ledger.calls, [])
        self.transport.rotate.assert_not_called()

    def test_reimport_namespace_and_identity_validation(self):
        self.run_refresh()
        reimport = self.reload(account=dict(ACCOUNT, cookie=NEW, refresh_token='matching-token'))
        self.assertNotEqual(self.journal.key, reimport.key)
        self.assertEqual(reimport.state['cookie'], NEW)
        with self.assertRaises(cr.CredentialError):
            self.reload(account=dict(ACCOUNT, uid=456))


class TransportTests(unittest.TestCase):
    def response(self, payload, set_cookies=()):
        response = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        response.headers = Message()
        for cookie in set_cookies:
            response.headers.add_header('Set-Cookie', cookie)
        return response

    def transport(self, responses):
        opener = Mock()
        opener.open.side_effect = responses
        return cr.RefreshTransport(opener, cloud.AccountGate(), 'test-agent')

    @patch('index.time.sleep')
    def test_protocol_crypto_cookies_and_confirmation_use_correct_tokens(self, _):
        private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        transport = self.transport([
            self.response(b'<div id="1-name">1234567890abcdef1234567890abcdef</div>'),
            self.response({'code': 0, 'data': {'status': 0, 'refresh_token': 'new-token'}},
                ('SESSDATA=private-new; HttpOnly; Path=/', 'bili_jct=new-csrf; Path=/', 'DedeUserID=123; Path=/')),
            self.response({'code': 0})])
        with patch('credential_refresh.PUBLIC_KEY', public):
            state = transport.rotate(cr.seed(ACCOUNT), 1234567890123)
        transport.confirm(state)
        requests = [call.args[0] for call in transport.opener.open.call_args_list]
        path = requests[0].full_url[len(cr.CORRESPOND):]
        plaintext = private.decrypt(bytes.fromhex(path), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        self.assertEqual(plaintext, b'refresh_1234567890123')
        refresh = urllib_parse(requests[1].data)
        confirm = urllib_parse(requests[2].data)
        self.assertEqual(refresh['csrf'], ['old-csrf'])
        self.assertEqual(confirm['csrf'], ['new-csrf'])
        self.assertEqual(confirm['refresh_token'], ['private-token'])
        self.assertIn('private-new', requests[2].get_header('Cookie'))
        self.assertEqual(cr.cookies(state['cookie'])['buvid3'], 'private-device')

    def test_rejects_unlisted_hosts_methods_and_paths(self):
        transport = self.transport([])
        for url, method in ((cr.INFO, 'POST'), (cr.REFRESH, 'GET'),
                (cr.CORRESPOND + 'aa', 'GET'), (cr.REFRESH + '?extra=1', 'POST'),
                ('https://passport.bilibili.com.evil.test/cookie/refresh', 'POST')):
            with self.assertRaises(cr.CredentialError):
                transport.request(url, OLD, method)
        transport.opener.open.assert_not_called()

    @patch('index.time.sleep')
    def test_provider_message_and_transport_secrets_never_escape(self, _):
        for response in (self.response({'code': -352, 'message': 'private-token private-old'}),
                         RuntimeError('private-token')):
            transport = self.transport([response])
            with self.assertRaises(cr.CredentialError) as exc:
                transport.info(OLD)
            self.assertNotIn('private-', str(exc.exception))
        self.assertEqual(transport.opener.open.call_count, 1)


def urllib_parse(data):
    import urllib.parse
    return urllib.parse.parse_qs(data.decode())


class IntegrationTests(unittest.TestCase):
    def test_credential_resolver_returns_rotated_account_to_queue_and_watch(self):
        settings = {'ENABLE_COOKIE_REFRESH': 'true', 'CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode()}
        with (patch('index.maintain', return_value=({'cookie': NEW, 'refresh_token': 'new'}, 'refreshed')),
                patch('index.Bili')):
            account, status = cloud.credential_account(ACCOUNT, settings, Ledger(), DAY, cloud.AccountGate())
        self.assertEqual(account['cookie'], NEW)
        self.assertEqual(status, 'refreshed')
        with patch('index.Bili') as factory, patch('index.watch', return_value={}), patch('index.alive', return_value=True):
            cloud.queue_watch(account, cloud.AccountGate(), 1, 2, {}, DAY, 99999)
        self.assertEqual(factory.call_args.args[0], NEW)

    def test_disabled_refresh_keeps_existing_account_and_does_not_read_storage(self):
        ledger = Mock()
        self.assertEqual(cloud.credential_account(ACCOUNT, {}, ledger, DAY, cloud.AccountGate()), (ACCOUNT, 'disabled'))
        ledger.read_progress.assert_not_called()

    def test_maintenance_filters_disabled_accounts_and_never_calls_tasks_or_notifications(self):
        settings = {'ENABLE_COOKIE_REFRESH': 'true', 'ACTIVE_ACCOUNT_UIDS': '123',
                    'BILIBILI_ACCOUNTS_JSON': json.dumps([ACCOUNT, dict(ACCOUNT, uid=456)])}
        with (patch('index.settings_from', return_value=settings), patch('index.ObsLedger'),
                patch('index.credential_account', return_value=(ACCOUNT, 'not_required')) as maintain,
                patch('index.run_account_queue') as queue, patch('index.report') as notify):
            result = cloud.handler({'mode': 'credential_maintain'}, None)
        self.assertEqual([a['uid'] for a in result['accounts']], [123])
        self.assertEqual(maintain.call_count, 1)
        queue.assert_not_called()
        notify.assert_not_called()
        self.assertNotIn('private-', json.dumps(result))
