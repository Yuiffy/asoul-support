"""FunctionGraph adapter for asoul-support. See README.md for deployment."""
import base64
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from asoul_x25kn import enter, heartbeat

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
CST = dt.timezone(dt.timedelta(hours=8))
SUI_ROOM = 25788785
SUI_UID = 1954091502
LIVE = 'https://api.live.bilibili.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
MIXIN = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13]


class TaskError(RuntimeError):
    pass


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def today():
    return dt.datetime.now(CST).date().isoformat()


def progress(data, kind):
    """Unknown schemas fail closed; a completed round is not necessarily the daily cap."""
    items = [t for t in data.get('task_info', []) if t.get('jump_type') == kind]
    if len(items) != 1:
        return None
    item = items[0]
    match = re.search(r'(\d+)\s*/\s*(\d+)', item.get('sub_title', ''))
    if not match:
        return None
    current, limit = map(int, match.groups())
    if not (0 <= current <= limit <= 100 and limit > 0):
        return None
    return current, limit, item


def pending(data, kind):
    p = progress(data, kind)
    return p is not None and p[0] < p[1]


def summary(data):
    return {kind: (list(p[:2]) if (p := progress(data, kind)) else None)
            for kind in ('feedLight', 'watchLive', 'sendDanmu', 'like')}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TaskError('Unexpected redirect; request stopped')


class Bili:
    def __init__(self, cookie, expected_uid):
        self.cookie = cookie
        jar = http.cookies.SimpleCookie()
        jar.load(cookie)
        self.cookies = {k: v.value for k, v in jar.items()}
        if not all(self.cookies.get(k) for k in ('SESSDATA', 'bili_jct')):
            raise TaskError('Missing SESSDATA or bili_jct')
        self.csrf = self.cookies['bili_jct']
        self.uid = int(expected_uid)
        self.salt = ''
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, params=None, method='GET', signed=False, form=None):
        if urllib.parse.urlsplit(url).hostname not in {
            'api.bilibili.com', 'api.live.bilibili.com', 'live-trace.bilibili.com'
        }:
            raise TaskError('Unexpected Bilibili host')
        params = dict(params or {})
        if signed:
            if not self.salt:
                raise TaskError('WBI key unavailable')
            params['wts'] = int(time.time())
            params = {k: re.sub(r"[!'()*]", '', str(v)) for k, v in params.items()}
            query = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote)
            params['w_rid'] = hashlib.md5((query + self.salt).encode()).hexdigest()
        if params:
            url += '?' + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        headers = {'User-Agent': UA, 'Cookie': self.cookie,
                   'Referer': f'https://live.bilibili.com/{SUI_ROOM}',
                   'Origin': 'https://live.bilibili.com'}
        body = None
        if method == 'POST':
            body = urllib.parse.urlencode(form or {}).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        # No automatic POST retries: a timeout may mean the server accepted it.
        try:
            with self.opener.open(req, timeout=15) as response:
                result = json.load(response)
        except Exception as exc:
            raise TaskError('Bilibili transport failure: ' + type(exc).__name__) from None
        if result.get('code') != 0:
            raise TaskError('Bilibili API rejected request, code=' + str(result.get('code')))
        if result.get('msg') in ('k', 'f'):
            raise TaskError('Danmaku filtered; stopped')
        return result.get('data') or {}

    def login(self):
        nav = self.request('https://api.bilibili.com/x/web-interface/nav')
        if nav.get('isLogin') is not True or int(nav.get('mid', 0)) != self.uid:
            raise TaskError('Account UID mismatch or expired cookie')
        imgs = nav['wbi_img']
        keys = ''.join(urllib.parse.urlsplit(imgs[k]).path.rsplit('/', 1)[-1].split('.')[0]
                       for k in ('img_url', 'sub_url'))
        if len(keys) < 64:
            raise TaskError('Invalid WBI keys')
        self.salt = ''.join(keys[i] for i in MIXIN)
        return {'uid': self.uid, 'name': nav.get('uname')}

    def room(self, room_id):
        return self.request(LIVE + '/room/v1/Room/get_info', {'room_id': room_id})

    def tasks(self, uid):
        return self.request(LIVE + '/xlive/app-ucenter/v1/fansMedal/GetActivatedMedalInfo',
                            {'target_id': uid, 'csrf': self.csrf, 'web_location': '444.260'})

    def medal_rooms(self):
        result = {}
        for page in range(1, 31):
            data = self.request(LIVE + '/xlive/app-ucenter/v1/fansMedal/panel',
                                {'page': page, 'page_size': 50})
            for item in (data.get('special_list') or []) + (data.get('list') or []):
                uid = int(item['medal']['target_id'])
                room = int(item.get('room_info', {}).get('room_id', 0))
                if room:
                    result[room] = uid
            if not data.get('page_info', {}).get('has_more'):
                return result
        raise TaskError('Medal pagination limit exceeded')

    def like(self, room_id, uid, count):
        return self.request(LIVE + '/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3',
                            {'room_id': room_id, 'anchor_id': uid, 'uid': self.uid,
                             'click_time': count, 'csrf': self.csrf, 'web_location': '444.8'},
                            method='POST', signed=True)

    def danmu(self, room_id, message):
        return self.request(LIVE + '/msg/send', {'web_location': '444.8'}, 'POST', True,
                            {'msg': message, 'roomid': room_id, 'bubble': 0, 'color': 16777215,
                             'mode': 1, 'room_type': 0, 'fontsize': 25, 'rnd': int(time.time()),
                             'csrf': self.csrf, 'csrf_token': self.csrf,
                             'statistics': '{"appId":100,"platform":5}'})

    def trace(self, action, form):
        return self.request('https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/' + action,
                            {**form, 'ua': UA, 'csrf': self.csrf, 'web_location': '444.8'},
                            method='POST', signed=True)

    def gift(self, gift_id, price):
        return self.request(LIVE + '/xlive/revenue/v1/gift/sendGold', method='POST', form={
            'uid': self.uid, 'gift_id': gift_id, 'gift_num': 1, 'price': price,
            'ruid': SUI_UID, 'biz_code': 'Live', 'biz_id': SUI_ROOM, 'platform': 'pc',
            'storm_beat_id': 0, 'send_ruid': 0, 'coin_type': 'gold', 'bag_id': 0,
            'rnd': int(time.time()), 'visit_id': '', 'csrf': self.csrf, 'csrf_token': self.csrf})


class ObsLedger:
    """Atomic append at position zero: only one caller can reserve an object.

    Reservation is never removed, including rejected or uncertain gift requests.
    Cold starts, concurrent invocations and retries therefore cannot pay twice.
    """
    def __init__(self, context, bucket):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
            raise TaskError('OBS_BUCKET is required for durable reservations')
        self.bucket = bucket
        self.context = context
        self.opener = urllib.request.build_opener(NoRedirect())

    def reserve(self, key, value):
        ak = self.context.getAccessKey()
        sk = self.context.getSecretKey()
        token = self.context.getSecurityToken()
        if not all((ak, sk, token)):
            raise TaskError('Function execution agency credentials unavailable')
        date = email.utils.formatdate(usegmt=True)
        content_type = 'application/json'
        resource = f'/{self.bucket}/{key}?append&position=0'
        canonical = f'POST\n\n{content_type}\n{date}\nx-obs-security-token:{token}\n{resource}'
        signature = base64.b64encode(hmac.new(sk.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
        req = urllib.request.Request(
            f'https://{self.bucket}.obs.cn-south-1.myhuaweicloud.com/{key}?append&position=0',
            data=(compact(value) + '\n').encode(), method='POST',
            headers={'Date': date, 'Content-Type': content_type, 'x-obs-security-token': token,
                     'Authorization': f'OBS {ak}:{signature}'})
        try:
            with self.opener.open(req, timeout=15) as response:
                if response.status != 200:
                    raise TaskError('OBS reservation rejected')
            return True
        except urllib.error.HTTPError as exc:
            # Both existing append objects and non-append objects must block duplicates.
            if exc.code == 409:
                return False
            raise TaskError('OBS reservation HTTP ' + str(exc.code)) from None
        except TaskError:
            raise
        except Exception as exc:
            raise TaskError('OBS reservation uncertain: ' + type(exc).__name__) from None


def maybe_gift(client, ledger, allow_paid, run_day):
    if not allow_paid:
        return 'disabled_for_account'
    if today() != run_day:
        return 'cross_day'
    now = dt.datetime.now(CST)
    if now.hour == 23 and now.minute >= 58:
        return 'midnight_guard'
    data = client.tasks(SUI_UID)
    task = progress(data, 'feedLight')
    if task is None or task[1] != 1:
        return 'unknown_gift_task'
    if task[0] != 0 or task[2].get('is_done') is not False:
        return 'already_done'
    gift = data.get('fans_club_gift_info') or {}
    gift_id, price = gift.get('gift_id'), gift.get('price')
    # Verified Sui fans-club gift; a changed ID/price requires review.
    if gift_id != 31164 or type(price) is not int or not 0 < price <= 100:
        return 'unexpected_gift_or_price'
    key = f'gifts/{client.uid}/{SUI_UID}/{run_day}.json'
    if not ledger.reserve(key, {'state': 'reserved', 'price': price, 'quantity': 1}):
        return 'already_reserved'
    # Recheck after taking the durable reservation to include manual gifts as late as possible.
    fresh = client.tasks(SUI_UID)
    task = progress(fresh, 'feedLight')
    if today() != run_day or task is None or task[0] != 0 or task[1] != 1 or task[2].get('is_done') is not False:
        return 'changed_after_reservation'
    if fresh.get('fans_club_gift_info') != gift:
        return 'gift_changed_after_reservation'
    client.gift(gift_id, price)
    time.sleep(5)
    verified = progress(client.tasks(SUI_UID), 'feedLight')
    return 'confirmed' if verified and verified[0] == 1 else 'sent_unconfirmed_no_retry'


def alive(day, deadline):
    return today() == day and time.monotonic() < deadline


def free_actions(client, room_id, uid, data, day, deadline, max_rounds, offline_only):
    for kind in ('like', 'sendDanmu'):
        for _ in range(max_rounds):
            if not alive(day, deadline) or data.get('reach_free_intimacy_limit') or not pending(data, kind):
                break
            room = client.room(room_id)
            live = room.get('live_status') == 1
            if int(room.get('uid', 0)) != uid:
                raise TaskError('Room owner mismatch')
            if (kind == 'like' and not live) or (kind == 'sendDanmu' and live and offline_only):
                break
            before = progress(data, kind)[0]
            if kind == 'like':
                item = progress(data, kind)[2]
                match = re.search(r'(\d+)', item.get('title', ''))
                if not match or not 1 <= int(match[1]) <= 60:
                    break
                remaining = int(match[1])
                while remaining > 0 and alive(day, deadline):
                    count = min(10, remaining)
                    client.like(room_id, uid, count)
                    remaining -= count
                    time.sleep(3)
            else:
                message = '岁己加油~' if uid == SUI_UID else '支持~'
                client.danmu(room_id, message)
            time.sleep(10)
            data = client.tasks(uid)
            if not progress(data, kind) or progress(data, kind)[0] <= before:
                LOG.warning('No confirmed %s progress for room %s; stopping this action', kind, room_id)
                break
    return data


def watch(client, room_id, uid, data, day, deadline):
    if data.get('reach_free_intimacy_limit') or not pending(data, 'watchLive'):
        return data
    info = client.room(room_id)
    if info.get('live_status') != 1:
        return data
    if int(info.get('uid', 0)) != uid:
        raise TaskError('Room owner mismatch')
    buvid = client.cookies.get('LIVE_BUVID')
    if not buvid:
        buvid = client.request('https://api.bilibili.com/x/frontend/finger/spi').get('b_3')
    if not buvid:
        raise TaskError('No Bilibili-issued device ID')
    device = [buvid, str(uuid.uuid4())]
    ids = [int(info['parent_area_id']), int(info['area_id']), 0, room_id]
    state = enter(client.trace, ids, device, uid)
    seq, last_check, last_progress, stalled = 1, time.monotonic(), progress(data, 'watchLive')[0], 0
    while alive(day, deadline):
        interval = int(state['heartbeat_interval'])
        if not 5 <= interval <= 300 or any(r not in range(6) for r in state['secret_rule']):
            raise TaskError('Unsupported heartbeat parameters')
        next_at = float(state['timestamp']) + interval
        delay = next_at - time.time()
        if delay < -5:
            raise TaskError('Heartbeat chain expired')
        if time.monotonic() + max(0, delay) + 20 >= deadline:
            break
        while delay > 0 and alive(day, deadline):
            time.sleep(min(delay, 1))
            delay = next_at - time.time()
        if not alive(day, deadline):
            break
        ids[2] = seq
        state = heartbeat(client.trace, ids, device, uid, state)
        seq += 1
        if time.monotonic() - last_check >= 180:
            # Verify actual Bilibili progress and ongoing live status during the session.
            if client.room(room_id).get('live_status') != 1:
                break
            data = client.tasks(uid)
            if data.get('reach_free_intimacy_limit') or not pending(data, 'watchLive'):
                break
            last_check = time.monotonic()
            current = progress(data, 'watchLive')[0]
            stalled = stalled + 1 if current <= last_progress else 0
            last_progress = current
            if stalled >= 7:
                LOG.warning('Watch progress did not increase for 21 minutes in room %s', room_id)
                break
    return client.tasks(uid)


def run_room(account, room_id, uid, day, deadline, ledger):
    client = Bili(account['cookie'], account['uid'])
    identity = client.login()
    data = client.tasks(uid)
    before = summary(data)
    slot = int(time.time()) // 1800
    if not ledger.reserve(f'runs/{client.uid}/{room_id}/{slot}.json', {'day': day}):
        return {'uid': client.uid, 'room': room_id, 'status': 'duplicate_slot'}
    allow_paid = (account.get('role') == 'primary' and account.get('allow_paid') is True and uid == SUI_UID
                  and str(client.uid) == os.environ.get('PAID_ACCOUNT_UID', '')
                  and os.environ.get('ENABLE_PAID_GIFT') == 'true')
    try:
        gift = maybe_gift(client, ledger, allow_paid, day) if uid == SUI_UID else 'disabled_other_room'
    except TaskError:
        gift = 'failed_or_uncertain_no_retry'
        LOG.warning('Gift attempt stopped for UID %s; no same-day retry', client.uid)
    data = client.tasks(uid)
    if not data.get('reach_free_intimacy_limit'):
        data = free_actions(client, room_id, uid, data, day, deadline,
                            10 if uid == SUI_UID else 1, offline_only=(uid != SUI_UID))
        data = watch(client, room_id, uid, data, day, deadline)
    result = {'account': identity, 'room': room_id, 'before': before, 'after': summary(data),
              'gift': gift, 'storage_full': bool(data.get('reach_free_intimacy_limit'))}
    LOG.info('%s', compact(result))
    return result


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    if event.get('mode') == 'health':
        return {'status': 'ready', 'base': 'asoul-support v4.1.1', 'version': 1,
                'actions_enabled': os.environ.get('ENABLE_ACTIONS') == 'true',
                'paid_enabled': os.environ.get('ENABLE_PAID_GIFT') == 'true'}
    accounts = json.loads(os.environ.get('BILIBILI_ACCOUNTS_JSON', '[]'))
    if not accounts:
        return {'status': 'needs_credentials'}
    if not isinstance(accounts, list) or len(accounts) > 2:
        raise TaskError('Expected one or two explicitly configured accounts')
    if sum(a.get('role') == 'primary' for a in accounts) != 1:
        raise TaskError('Configure exactly one primary account')
    identities, targets = [], []
    seen = set()
    for account in accounts:
        uid = int(account['uid'])
        if uid in seen:
            raise TaskError('Duplicate account UID')
        seen.add(uid)
        client = Bili(account['cookie'], uid)
        identity = client.login()
        data = client.tasks(SUI_UID)
        identities.append({'account': identity, 'tasks': summary(data),
                           'storage_full': bool(data.get('reach_free_intimacy_limit'))})
        targets.append((account, SUI_ROOM, SUI_UID))
        # Optional secondary rooms: existing medals only, explicit per-account enable, capped.
        if account.get('other_medals') is True:
            banned = {int(x) for x in account.get('banned_uids', [])}
            others = [(room, anchor) for room, anchor in client.medal_rooms().items()
                      if room != SUI_ROOM and anchor not in banned]
            # Rotate pages so a large medal list is eventually covered.
            if others:
                offset = (int(time.time()) // 1800 * 5) % len(others)
                others = (others[offset:] + others[:offset])[:5]
            targets.extend((account, room, anchor) for room, anchor in others)
    if event.get('mode') == 'inspect' or os.environ.get('ENABLE_ACTIONS') != 'true':
        return {'status': 'inspection_only', 'accounts': identities}
    ledger = ObsLedger(context, os.environ.get('OBS_BUCKET', ''))
    # Finish all workers before the next half-hour boundary, including delayed/retried events.
    slot_end = (int(time.time()) // 1800 + 1) * 1800 - 30
    budget = min(1680, slot_end - time.time())
    if budget < 90:
        return {'status': 'too_late_in_slot'}
    day, deadline = today(), time.monotonic() + budget
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as executor:
        futures = [executor.submit(run_room, a, r, u, day, deadline, ledger) for a, r, u in targets]
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                # Do not log raw request URLs, cookies or SDK credential objects.
                message = str(exc) if isinstance(exc, TaskError) else type(exc).__name__
                LOG.error('Worker stopped: %s', message)
                results.append({'status': 'error', 'reason': message})
    return {'status': 'finished', 'day': day, 'results': results}
