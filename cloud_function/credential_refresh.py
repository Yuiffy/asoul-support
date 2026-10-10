"""Bilibili refresh and authenticated encrypted OBS credential journal.

Network and storage adapters are injected. A refresh POST is never retried after
an uncertain outcome. Only a durably saved new credential may be confirmed.
"""
import hashlib
import http.cookies
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

INFO = 'https://passport.bilibili.com/x/passport-login/web/cookie/info'
REFRESH = 'https://passport.bilibili.com/x/passport-login/web/cookie/refresh'
CONFIRM = 'https://passport.bilibili.com/x/passport-login/web/confirm/refresh'
CORRESPOND = 'https://www.bilibili.com/correspond/1/'
PUBLIC_KEY = b'''-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----'''


class CredentialError(RuntimeError):
    pass


def cookies(value):
    jar = http.cookies.SimpleCookie()
    jar.load(value)
    return {key: item.value for key, item in jar.items()}


def seed(account):
    try:
        jar = cookies(account['cookie'])
        value = {'uid': int(account['uid']), 'cookie': account['cookie'],
                 'refresh_token': next((account.get(k) for k in
                     ('refresh_token', 'ac_time_value', 'acTimeValue') if account.get(k)),
                     jar.get('ac_time_value', ''))}
        validate(value)
        return value
    except Exception:
        raise CredentialError('Credential UID mismatch or missing login fields') from None


def validate(value):
    try:
        jar = cookies(value['cookie'])
        if (int(jar.get('DedeUserID', 0)) != int(value['uid']) or
                not all(jar.get(k) for k in ('SESSDATA', 'bili_jct')) or
                not isinstance(value.get('refresh_token'), str)):
            raise ValueError('Invalid fields')
    except Exception:
        raise CredentialError('Credential UID mismatch or missing login fields') from None


class EncryptedJournal:
    def __init__(self, ledger, account, key, read_only=False):
        # Lazy imports let deployments with refresh disabled retain compatibility.
        self.initial = seed(account)
        self.uid = int(account['uid'])
        fingerprint = hashlib.sha256(json.dumps(self.initial, sort_keys=True).encode()).hexdigest()[:24]
        self.key = f'runs/daily/credentials/{self.uid}/{fingerprint}.jsonl'
        self.ledger, self.position = ledger, 0
        try:
            from cryptography.fernet import Fernet
            self.cipher = Fernet(key.encode())
            # Read-only diagnostics must not create missing OBS objects.
            raw = (ledger._progress_request(self.key) if read_only else ledger.read_progress(self.key))
            self.state = dict(self.initial, phase='ready', checked_day='')
            if raw is not None:
                if not raw.endswith(b'\n'):
                    raise ValueError('Incomplete journal')
                self.position = len(raw)
                for line in raw.splitlines():
                    record = json.loads(line)
                    if record == {'type': 'init', 'schema': 1}:
                        continue
                    if record.get('schema') != 1:
                        raise ValueError('Invalid credential journal')
                    state = json.loads(self.cipher.decrypt(record['encrypted'].encode()))
                    validate(state)
                    if state['uid'] != self.uid or state.get('phase') not in ('ready', 'refresh_started', 'pending_confirm'):
                        raise ValueError('Invalid credential state')
                    self.state = state
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Encrypted credential journal unavailable; account stopped') from None
        self.read_only = read_only

    def save(self, state):
        if self.read_only:
            raise CredentialError('Read-only credential journal')
        validate(state)
        if state['uid'] != self.uid:
            raise CredentialError('Credential journal UID mismatch')
        record = {'schema': 1, 'encrypted': self.cipher.encrypt(
            json.dumps(state, separators=(',', ':')).encode()).decode()}
        data = (json.dumps(record, separators=(',', ':')) + '\n').encode()
        try:
            self.ledger.append_progress(self.key, data, self.position)
        except Exception:
            raise CredentialError('Credential persistence uncertain; reload before further work') from None
        self.position += len(data)
        self.state = dict(state)


class RefreshCsrfParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture, self.value = False, ''

    def handle_starttag(self, tag, attrs):
        self.capture = tag == 'div' and dict(attrs).get('id') == '1-name'

    def handle_data(self, data):
        if self.capture:
            self.value += data

    def handle_endtag(self, tag):
        self.capture = False


class RefreshTransport:
    def __init__(self, opener, gate, user_agent):
        self.opener, self.gate, self.user_agent = opener, gate, user_agent

    def request(self, url, cookie, method='GET', form=None, html=False):
        allowed = ((url == INFO and method == 'GET' and not html) or
                   (url in (REFRESH, CONFIRM) and method == 'POST' and not html) or
                   (re.fullmatch(re.escape(CORRESPOND) + r'[a-f0-9]{256}', url) and method == 'GET' and html))
        if not allowed:
            raise CredentialError('Unexpected credential endpoint')
        endpoint = urllib.parse.urlsplit(url).path
        headers = {'Cookie': cookie, 'User-Agent': self.user_agent,
                   'Referer': 'https://www.bilibili.com/', 'Origin': 'https://www.bilibili.com'}
        body = None
        if method == 'POST':
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urllib.parse.urlencode(form or {}).encode()
        try:
            with self.gate.slot(endpoint):
                self.gate.check(endpoint)
                with self.opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=15) as response:
                    raw = response.read(512 * 1024 + 1)
                    if len(raw) > 512 * 1024:
                        raise CredentialError('Credential response exceeds limit')
                    new = http.cookies.SimpleCookie()
                    for header in response.headers.get_all('Set-Cookie', []):
                        new.load(header)
                    if html:
                        return raw.decode('utf-8'), {}
                    result = json.loads(raw)
                    if result.get('code') != 0:
                        self.gate.rejected(result.get('code'), endpoint)
                        # Provider message could echo tokens; only return numeric code.
                        raise CredentialError(f'Credential endpoint rejected: code={result.get("code")}')
                    return result.get('data') or {}, {k: v.value for k, v in new.items()}
        except CredentialError:
            raise
        except Exception:
            raise CredentialError('Credential request failed; outcome may be uncertain') from None

    def info(self, cookie):
        return self.request(INFO, cookie)[0]

    def prepare(self, state, timestamp):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_public_key(PUBLIC_KEY)
        path = key.encrypt(f'refresh_{timestamp}'.encode(), padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).hex()
        page, _ = self.request(CORRESPOND + path, state['cookie'], html=True)
        parser = RefreshCsrfParser()
        parser.feed(page)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', parser.value):
            raise CredentialError('Refresh CSRF unavailable')
        return parser.value

    def rotate(self, state, refresh_csrf):
        jar = cookies(state['cookie'])
        data, updated = self.request(REFRESH, state['cookie'], 'POST', {
            'csrf': jar['bili_jct'], 'refresh_csrf': refresh_csrf,
            'source': 'main_web', 'refresh_token': state['refresh_token']})
        if data.get('status') != 0 or not data.get('refresh_token') or not all(
                updated.get(k) for k in ('SESSDATA', 'bili_jct', 'DedeUserID')):
            raise CredentialError('Incomplete refresh response; account stopped')
        # Preserve device cookies. This header is used on all allowlisted API hosts,
        # so browser cross-domain SSO cookie setting is unnecessary here.
        jar.update(updated)
        fresh = dict(state, cookie='; '.join(k + '=' + v for k, v in jar.items()),
                     refresh_token=data['refresh_token'], old_refresh_token=state['refresh_token'],
                     phase='pending_confirm')
        validate(fresh)
        return fresh

    def confirm(self, state):
        return self.request(CONFIRM, state['cookie'], 'POST', {
            'csrf': cookies(state['cookie'])['bili_jct'], 'refresh_token': state['old_refresh_token']})


def maintain(journal, transport, day, verify):
    state = dict(journal.state)
    if state['phase'] == 'refresh_started':
        raise CredentialError('Previous refresh outcome uncertain; reimport matching cookie and token')
    if state['phase'] == 'ready' and state.get('checked_day') == day:
        return state, 'checked_today'
    if state['phase'] == 'ready':
        verify(state['cookie'])
        info = transport.info(state['cookie'])
        if type(info.get('refresh')) is not bool:
            raise CredentialError('Cookie refresh recommendation unavailable')
        if not info['refresh']:
            journal.save(dict(state, checked_day=day))
            return journal.state, 'not_required'
        if not state['refresh_token']:
            raise CredentialError('Refresh required but matching refresh token missing')
        timestamp = info.get('timestamp')
        if type(timestamp) is not int or timestamp <= 0:
            raise CredentialError('Refresh timestamp unavailable')
        # A failed read-only preparation is safe to retry. Fence only the POST.
        refresh_csrf = transport.prepare(state, timestamp)
        journal.save(dict(state, phase='refresh_started'))
        state = transport.rotate(state, refresh_csrf)
        journal.save(state)
    # A failed confirmation can resume using the saved NEW cookie and OLD token.
    verify(state['cookie'])
    transport.confirm(state)
    state = dict(state, phase='ready', checked_day=day)
    state.pop('old_refresh_token', None)
    journal.save(state)
    return state, 'refreshed'
