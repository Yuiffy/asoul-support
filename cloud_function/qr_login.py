"""Explicit QR login helper. Credentials are saved locally and never printed."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

BASE = 'https://passport.bilibili.com/x/passport-login/web/qrcode/'
parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=['generate', 'poll'])
parser.add_argument('directory', type=Path, help='An ignored local directory outside source control')
args = parser.parse_args()
args.directory.mkdir(parents=True, exist_ok=True)
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
    with opener.open(req, timeout=15) as resp:
        body = json.load(resp)
    if body.get('code') != 0:
        raise RuntimeError('Bilibili login API rejected the request')
    return body['data']

if args.mode == 'generate':
    import qrcode
    data = get(BASE + 'generate')
    state = args.directory / 'qr-state.json'
    state.write_text(json.dumps(data), encoding='utf-8')
    os.chmod(state, 0o600)
    image_path = args.directory / 'login-qr.png'
    qrcode.make(data['url']).save(image_path)
    print(image_path.resolve())
else:
    state = json.loads((args.directory / 'qr-state.json').read_text(encoding='utf-8'))
    data = get(BASE + 'poll?' + urllib.parse.urlencode({'qrcode_key': state['qrcode_key']}))
    code = data.get('code')
    if code == 0:
        cookies = {c.name: c.value for c in jar if c.domain.lstrip('.').endswith('bilibili.com')}
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(data.get('url', '')).query)
        for name in ('SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5'):
            if name not in cookies and name in query:
                cookies[name] = query[name][0]
        if not all(cookies.get(k) for k in ('SESSDATA', 'bili_jct', 'DedeUserID')):
            raise RuntimeError('Login succeeded without all required cookies')
        path = args.directory / 'accounts.local.json'
        path.write_text(json.dumps({'uid': int(cookies['DedeUserID']), 'cookie': '; '.join(k+'='+v for k,v in cookies.items())}), encoding='utf-8')
        os.chmod(path, 0o600)
        print(json.dumps({'status': 'logged_in', 'uid': int(cookies['DedeUserID'])}))
    else:
        print(json.dumps({'status': {86101:'waiting_for_scan',86090:'waiting_for_confirmation',86038:'expired'}.get(code,'rejected'), 'code':code}))
