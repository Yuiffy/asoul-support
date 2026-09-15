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
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from asoul_x25kn import enter, heartbeat
from wecom_notify import report, send_notice
from daily_progress import DailyProgress, ProgressError

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


class ApiError(TaskError):
    def __init__(self, code, endpoint, message):
        self.code, self.endpoint = code, endpoint
        super().__init__(f'{endpoint}: code={code}; {message}')


class AccountGate:
    """Shared across one account's queue and watch workers, never a room-local limit."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0
        self.last_danmu = 0.0
        self.stopped = False
        self.risk_code = None
        self.disabled = set()

    def check(self, endpoint):
        if self.stopped:
            raise TaskError('Account paused after risk control; deferred until next scheduled run')
        if endpoint in self.disabled:
            raise TaskError('Endpoint paused after rejection: ' + endpoint)

    def rejected(self, code, endpoint):
        if code in (-352, -412, -101):
            self.stopped = True
            self.risk_code = code
        if code == 10030:
            self.disabled.add(endpoint)



def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def today():
    return dt.datetime.now(CST).date().isoformat()


def settings_from(context):
    settings = {}
    for key in ('BILIBILI_ACCOUNTS_JSON', 'ENABLE_ACTIONS', 'ENABLE_PAID_GIFT', 'PAID_ACCOUNT_UID', 'OBS_BUCKET', 'WECOM_WEBHOOK_URL'):
        value = context.getUserData(key) if context is not None and hasattr(context, 'getUserData') else None
        settings[key] = value if value is not None else os.environ.get(key, '')
    return settings


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


def merge_progress(previous, current):
    merged = current.copy()
    for key, old in previous.items():
        new = current.get(key)
        if old and new and old[1] == new[1]:
            merged[key] = [max(old[0], new[0]), new[1]]
    return merged


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TaskError('Unexpected redirect; request stopped')


class Bili:
    def __init__(self, cookie, expected_uid, gate=None):
        self.cookie = cookie
        self.gate = gate or AccountGate()
        jar = http.cookies.SimpleCookie()
        jar.load(cookie)
        self.cookies = {k: v.value for k, v in jar.items()}
        if not all(self.cookies.get(k) for k in ('SESSDATA', 'bili_jct')):
            raise TaskError('Missing SESSDATA or bili_jct')
        self.csrf = self.cookies['bili_jct']
        self.uid = int(expected_uid)
        self.salt = ''
        self.watch_seconds = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url, params=None, method='GET', signed=False, form=None):
        if urllib.parse.urlsplit(url).hostname not in {
            'api.bilibili.com', 'api.live.bilibili.com', 'live-trace.bilibili.com'
        }:
            raise TaskError('Unexpected Bilibili host')
        endpoint = urllib.parse.urlsplit(url).path
        self.gate.check(endpoint)
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
        # Wait for the account's danmaku cooldown outside the network lock so
        # a scheduled heartbeat is not blocked behind a 30-second chat wait.
        if endpoint == '/msg/send':
            while self.gate.last_danmu + 30 > time.monotonic():
                self.gate.check(endpoint)
                time.sleep(min(1, self.gate.last_danmu + 30 - time.monotonic()))
        # No automatic POST retries: a timeout may mean the server accepted it.
        try:
            with self.gate.lock:
                self.gate.check(endpoint)
                delay = max(0, self.gate.last + 1.5 - time.monotonic())
                if delay > 0:
                    time.sleep(delay)
                self.gate.last = time.monotonic()
                if endpoint == '/msg/send':
                    self.gate.last_danmu = self.gate.last
                with self.opener.open(req, timeout=15) as response:
                    result = json.load(response)
                if result.get('code') != 0:
                    self.gate.rejected(result.get('code'), endpoint)
        except TaskError:
            raise
        except Exception as exc:
            raise TaskError(endpoint + ': Bilibili transport failure: ' + type(exc).__name__) from None
        if result.get('code') != 0:
            message = str(result.get('message') or result.get('msg') or 'no message')
            for secret in self.cookies.values():
                if len(secret) >= 4:
                    message = message.replace(secret, '[redacted]')
            message = re.sub(r'https?://\S+|[A-Za-z0-9_%-]{24,}', '[redacted]', message)[:140]
            raise ApiError(result.get('code'), endpoint, message)
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
        self.gate.check('/msg/send')
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
        ak = self.context.getSecurityAccessKey()
        sk = self.context.getSecuritySecretKey()
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

    def _progress_request(self, key, method='GET', data=None, position=None):
        if not key.startswith('runs/daily/') or not re.fullmatch(r'[A-Za-z0-9_./-]+', key) or '..' in key:
            raise ProgressError('Invalid daily progress key')
        ak = self.context.getSecurityAccessKey()
        sk = self.context.getSecuritySecretKey()
        token = self.context.getSecurityToken()
        if not all((ak, sk, token)):
            raise ProgressError('Function execution agency credentials unavailable')
        date = email.utils.formatdate(usegmt=True)
        content_type = 'application/x-ndjson' if method == 'POST' else ''
        suffix = f'?append&position={position}' if method == 'POST' else ''
        resource = f'/{self.bucket}/{key}{suffix}'
        canonical = f'{method}\n\n{content_type}\n{date}\nx-obs-security-token:{token}\n{resource}'
        signature = base64.b64encode(hmac.new(sk.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
        headers = {'Date': date, 'x-obs-security-token': token, 'Authorization': f'OBS {ak}:{signature}'}
        if content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(f'https://{self.bucket}.obs.cn-south-1.myhuaweicloud.com/{key}{suffix}',
                                     data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=15) as response:
                if response.status != 200:
                    raise ProgressError('OBS daily progress response rejected')
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise ProgressError('OBS daily journal exceeds size limit')
                return body
        except urllib.error.HTTPError as exc:
            if method == 'GET' and exc.code == 404:
                return None
            raise ProgressError('OBS daily progress HTTP ' + str(exc.code)) from None
        except ProgressError:
            raise
        except Exception as exc:
            raise ProgressError('OBS daily progress request failed: ' + type(exc).__name__) from None

    def read_progress(self, key):
        return self._progress_request(key)

    def append_progress(self, key, data, position):
        self._progress_request(key, 'POST', data, position)


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
        endpoint = '/msg/send' if kind == 'sendDanmu' else '/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3'
        if endpoint in client.gate.disabled:
            continue
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
    watch_deadline = min(deadline, time.monotonic() + (9600 if uid == SUI_UID else 960))
    deadline = watch_deadline
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
        client.watch_seconds += interval
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


def queue_error(client, identity, room, before, exc):
    return {'account': identity, 'room': room, 'before': before, 'after': before,
            'status': 'error', 'reason': str(exc) if isinstance(exc, TaskError) else type(exc).__name__,
            'watch_seconds': client.watch_seconds}


def run_account_queue(account, day, deadline, ledger, settings):
    gate = AccountGate()
    identity = {'uid': account['uid']}
    metadata = {'account': identity, 'pending_rooms': 0}
    rows = {}
    daily = None
    try:
        daily = DailyProgress(ledger, account, settings, day)
        if daily.header:
            identity = daily.header['identity']
            metadata.update(account=identity, medal_rooms_total=daily.header['medal_rooms_total'],
                            eligible_rooms=len(daily.header['targets']), cached_completed_rooms=len(daily.done),
                            queue_source='daily_remaining')
            if not daily.remaining():
                metadata.update(status='daily_complete', daily_completed_rooms=len(daily.done),
                                remaining_rooms=0, incomplete_rooms=0)
                return metadata, []
        client = Bili(account['cookie'], account['uid'], gate)
        identity = client.login()
        metadata['account'] = identity
        if daily.header is None:
            medals = client.medal_rooms() if account.get('other_medals') else {SUI_ROOM: SUI_UID}
            banned = {int(x) for x in account.get('banned_uids', [])}
            roster = [(SUI_ROOM, SUI_UID)] + [(r,u) for r,u in medals.items() if r != SUI_ROOM and u not in banned]
            daily.initialize(identity, roster, len(medals))
            metadata.update(medal_rooms_total=len(medals), eligible_rooms=len(roster),
                            cached_completed_rooms=0, queue_source='daily_full')
        targets = daily.remaining()

        def checkpoint(room):
            try:
                daily.record(room, rows[room], today())
            except ProgressError:
                gate.stopped = True
                raise

        watch_targets = []
        primary_future = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as watcher:
            for room, uid in targets:
                if gate.stopped or not alive(day, deadline):
                    break
                before = {}
                try:
                    data = client.tasks(uid)
                    before = summary(data)
                    gift = 'disabled_other_room'
                    if room == SUI_ROOM:
                        paid = (account.get('role') == 'primary' and account.get('allow_paid') is True
                                and str(client.uid) == settings.get('PAID_ACCOUNT_UID')
                                and settings.get('ENABLE_PAID_GIFT') == 'true')
                        try:
                            gift = maybe_gift(client, ledger, paid, day)
                        except TaskError as exc:
                            gift = 'failed_or_uncertain_no_retry'
                            if gate.stopped:
                                raise
                        data = client.tasks(uid)
                    row = {'account': identity, 'room': room, 'before': before, 'after': summary(data),
                           'gift': gift, 'storage_full': bool(data.get('reach_free_intimacy_limit')), 'watch_seconds':0}
                    rows[room] = row
                    if row['storage_full']:
                        checkpoint(room)
                        continue
                    # One round per room first: the entire medal list is visited before repeats.
                    data = free_actions(client, room, uid, data, day, deadline, 1, offline_only=(uid != SUI_UID))
                    row['after'] = summary(data)
                    if pending(data, 'watchLive') and client.room(room).get('live_status') == 1:
                        if room == SUI_ROOM:
                            primary_future = watcher.submit(queue_watch, account, gate, room, uid, data, day, deadline)
                        else:
                            watch_targets.append((room,uid,data))
                    checkpoint(room)
                except TaskError as exc:
                    row = rows.get(room, queue_error(client, identity, room, before, exc))
                    row.update(status='error',reason=str(exc))
                    rows[room] = row
                    checkpoint(room)
            metadata['pending_rooms'] = len(targets) - len(rows)
            # Sweep remaining free rounds fairly; never retry a rejected room during this run.
            for _ in range(9):
                changed = False
                for room, uid in targets:
                    if gate.stopped or not alive(day, deadline):
                        break
                    row = rows.get(room)
                    if not row or row.get('status') == 'error' or row.get('storage_full'):
                        continue
                    if not any(row['after'].get(k) and row['after'][k][0] < row['after'][k][1]
                               for k in ('like','sendDanmu')):
                        continue
                    try:
                        data = client.tasks(uid)
                        data = free_actions(client,room,uid,data,day,deadline,1,offline_only=(uid != SUI_UID))
                        updated = summary(data)
                        changed |= updated != row['after']
                        row['after'] = updated
                        checkpoint(room)
                    except TaskError as exc:
                        row.update(status='error',reason=str(exc))
                if not changed or gate.stopped or not alive(day,deadline):
                    break
            # Other watch rooms are queued, one at a time, alongside the Sui watch worker.
            for room, uid, data in watch_targets:
                if gate.stopped or not alive(day, deadline):
                    break
                try:
                    update, seconds = queue_watch(account,gate,room,uid,data,day,deadline)
                    rows[room].update(after=summary(update),watch_seconds=seconds)
                    checkpoint(room)
                except TaskError as exc:
                    rows[room].update(status='error',reason=str(exc))
            if primary_future:
                try:
                    update, seconds = primary_future.result()
                    rows[SUI_ROOM].update(after=merge_progress(rows[SUI_ROOM]['after'], summary(update)),watch_seconds=seconds)
                    checkpoint(SUI_ROOM)
                except TaskError as exc:
                    rows[SUI_ROOM].update(status='error',reason=str(exc))
        metadata['paused_by_risk'] = gate.risk_code in (-352, -412)
        metadata['incomplete_rooms'] = sum(not all(row.get('after',{}).get(k) and row['after'][k][0]>=row['after'][k][1]
                                                   for k in ('watchLive','sendDanmu','like')) for row in rows.values())
    except (TaskError, ProgressError) as exc:
        metadata.update(status='error',reason=str(exc),paused_by_risk=gate.risk_code in (-352, -412))
    except Exception as exc:
        metadata.update(status='error',reason=type(exc).__name__)
    if daily is not None and daily.header is not None:
        metadata.update(daily_completed_rooms=len(daily.done), remaining_rooms=len(daily.remaining()))
    return metadata, list(rows.values())


def queue_watch(account, gate, room, uid, data, day, deadline):
    if not alive(day, deadline) or gate.stopped:
        return data, 0
    worker = Bili(account['cookie'], account['uid'], gate)
    worker.login()
    result = watch(worker,room,uid,data,day,deadline)
    return result, worker.watch_seconds


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    settings = settings_from(context)
    if event.get('mode') == 'progress_check':
        ledger = ObsLedger(context, settings.get('OBS_BUCKET', ''))
        probe = {'uid': int(uuid.uuid4().hex[:14], 16), 'role': 'secondary'}
        first = DailyProgress(ledger, probe, {}, today())
        first.initialize({'uid': probe['uid'], 'name': 'progress-check'}, [(1, 2)], 1)
        first.record(1, {'after': {k:[10,10] for k in ('watchLive','sendDanmu','like')}}, today())
        resumed = DailyProgress(ledger, probe, {}, today())
        if resumed.remaining() or len(resumed.done) != 1:
            raise ProgressError('Daily progress read-back failed')
        return {'status': 'daily_progress_verified', 'cached_completed_rooms': 1, 'remaining_rooms': 0}
    if event.get('mode') == 'progress_state':
        ledger = ObsLedger(context, settings.get('OBS_BUCKET', ''))
        accounts = json.loads(settings.get('BILIBILI_ACCOUNTS_JSON') or '[]')
        result = []
        for account in accounts:
            state = DailyProgress(ledger, account, settings, today())
            result.append({'uid': account['uid'], 'initialized': state.header is not None,
                           'completed': len(state.done), 'remaining': len(state.remaining()) if state.header else None})
        return {'version': 'queue-v3', 'day': today(), 'accounts': result}
    if event.get('mode') == 'notify_test':
        ledger = ObsLedger(context, settings.get('OBS_BUCKET', ''))
        status = send_notice(settings, ledger, today() + '-test-summary-v2',
                             '岁己云函数企微通知测试\n每轮结束按账号汇总：覆盖房间数、日任务已满数、有进展数、跳过数和异常数，附岁己进度及灯牌结果。\n仅主账号给岁己送灯牌，每天最多 1 个、1 电池；副账号和其他主播只做免费任务。')
        return {'status': 'notification_test', 'notification': status}
    if event.get('mode') == 'ledger_check':
        ledger = ObsLedger(context, settings.get('OBS_BUCKET', ''))
        key = 'runs/_probe/' + uuid.uuid4().hex + '.json'
        first = ledger.reserve(key, {'check': 'atomic_reservation'})
        second = ledger.reserve(key, {'check': 'must_not_overwrite'})
        if first is not True or second is not False:
            raise TaskError('Durable reservation check failed')
        return {'status': 'ledger_verified', 'first_reserved': first, 'duplicate_blocked': not second}
    if event.get('mode') == 'health':
        return {'status': 'ready', 'base': 'asoul-support v4.1.1', 'version': 'queue-v3',
                'actions_enabled': settings.get('ENABLE_ACTIONS') == 'true',
                'paid_enabled': settings.get('ENABLE_PAID_GIFT') == 'true',
                'wecom_configured': bool(settings.get('WECOM_WEBHOOK_URL'))}
    accounts = json.loads(settings.get('BILIBILI_ACCOUNTS_JSON') or '[]')
    if not accounts:
        return {'status': 'needs_credentials'}
    if not isinstance(accounts, list) or len(accounts) > 2:
        raise TaskError('Expected one or two explicitly configured accounts')
    if sum(a.get('role') == 'primary' for a in accounts) != 1:
        raise TaskError('Configure exactly one primary account')
    if event.get('mode') == 'inspect' or settings.get('ENABLE_ACTIONS') != 'true':
        identities = []
        for account in accounts:
            try:
                client = Bili(account['cookie'], account['uid'])
                identity = client.login()
                data = client.tasks(SUI_UID)
                identities.append({'account': identity, 'tasks': summary(data),
                                   'storage_full': bool(data.get('reach_free_intimacy_limit'))})
            except TaskError as exc:
                identities.append({'account': {'uid': account['uid']}, 'status': 'error', 'reason': str(exc)})
        return {'status': 'inspection_only', 'version': 'queue-v3', 'accounts': identities}
    if len({int(a['uid']) for a in accounts}) != len(accounts):
        raise TaskError('Duplicate account UID')
    requested = event.get('max_seconds', 10800)
    if type(requested) not in (int, float) or not 90 <= requested <= 10800:
        raise TaskError('max_seconds must be between 90 and 10800')
    # Respect the deployed timeout even before the console is upgraded.
    if context is not None and hasattr(context, 'getRemainingTimeInMilliSeconds'):
        requested = min(requested, max(0, context.getRemainingTimeInMilliSeconds() / 1000 - 60))
    day, round_id = today(), int(time.time()) // 14400
    ledger = ObsLedger(context, settings.get('OBS_BUCKET', ''))
    if not ledger.reserve(f'runs/queue-v2/{round_id}.json', {'day': day}):
        return {'status': 'duplicate_run', 'version': 'queue-v3'}
    identities, results = [], []
    deadline = time.monotonic() + requested
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_account_queue, a, day, deadline, ledger, settings) for a in accounts]
        for future in concurrent.futures.as_completed(futures):
            identity, rows = future.result()
            identities.append(identity)
            results.extend(rows)
    return {'status': 'finished', 'version': 'queue-v3', 'day': day, 'accounts': identities,
            'results': results, 'notifications': report(settings, ledger, day, identities, results, round_id)}
