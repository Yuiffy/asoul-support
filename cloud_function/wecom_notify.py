"""Small WeCom notifier with durable once-per-event delivery attempts."""
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request

LOG = logging.getLogger(__name__)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('WeCom redirect refused')


def send_notice(settings, ledger, key, text):
    url = settings.get('WECOM_WEBHOOK_URL', '')
    if not url:
        return 'disabled'
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'qyapi.weixin.qq.com'
            or parsed.path != '/cgi-bin/webhook/send'
            or not urllib.parse.parse_qs(parsed.query).get('key')):
        return 'invalid_webhook'
    try:
        destination = hashlib.sha256(url.encode()).hexdigest()[:16]
        if not ledger.reserve('runs/notifications/' + destination + '/' + key + '.json', {'event': key}):
            return 'already_attempted'
        # Plain text avoids mentions/markup from account names or error strings.
        content = text.encode('utf-8')[:2000].decode('utf-8', errors='ignore')
        body = json.dumps({'msgtype': 'text', 'text': {'content': content}}, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as response:
            result = json.load(response)
        return 'sent' if result.get('errcode') == 0 else 'rejected'
    except Exception as exc:
        LOG.warning('WeCom notification failed: %s', type(exc).__name__)
        return 'failed_or_uncertain'


def events(day, identities, results, primary_uid, round_id=None):
    if not results and not any(r.get('status') == 'error' for r in identities):
        return []
    round_id = str(round_id if round_id is not None else int(time.time()) // 14400)
    accounts = {str(r.get('account', {}).get('uid')): r for r in identities}
    for row in results:
        account = row.get('account') or {'uid': row.get('uid', 'unknown')}
        accounts.setdefault(str(account.get('uid')), {'account': account})
    rooms = {r['room'] for r in results if r.get('room')}
    lines = ['直播亲密度任务 · 本轮统计', day,
             f'本轮检查：{len(results)}次，涉及{len(rooms)}个直播间（两个账号分别统计）']
    for uid, identity in accounts.items():
        name = identity.get('account', {}).get('name', uid)
        rows = [r for r in results if str((r.get('account') or {}).get('uid', r.get('uid', 'unknown'))) == uid]
        if identity.get('status') == 'error':
            lines += ['', str(name) + '：账号检查失败', str(identity.get('reason', 'unknown'))[:120]]
            continue
        full, improved, storage, duplicate, errors, seconds = 0, 0, 0, 0, 0, 0
        for row in rows:
            after, before = row.get('after', {}), row.get('before', {})
            if all(after.get(k) and after[k][0] >= after[k][1] for k in ('watchLive','sendDanmu','like')):
                full += 1
            if any(after.get(k) and before.get(k) and after[k][0] > before[k][0]
                   for k in ('watchLive','sendDanmu','like','feedLight')):
                improved += 1
            storage += bool(row.get('storage_full'))
            duplicate += row.get('status') == 'duplicate_slot'
            errors += row.get('status') == 'error'
            seconds += row.get('watch_seconds', 0)
        total = identity.get('medal_rooms_total')
        coverage = f'{len(rows)}/{total}' if total is not None else str(len(rows))
        lines += ['', str(name) + f'：本轮覆盖{coverage}个持牌直播间',
                  f'免费日任务已满{full}间；本轮有进展{improved}间',
                  f'储蓄满跳过{storage}间；重复跳过{duplicate}间；异常{errors}间',
                  f'已接受观看心跳：{seconds}秒']
        lines.append(f'尚未检查{identity.get("pending_rooms",0)}间；已检查但未满{identity.get("incomplete_rooms",0)}间')
        if identity.get('paused_by_risk'):
            lines.append('账号触发风控，本次队列已暂停，未继续重试。')
        sui = next((r for r in rows if r.get('room') == 25788785 and 'after' in r), None)
        if sui:
            values = []
            for kind, label in [('watchLive','观看'),('sendDanmu','弹幕'),('like','点赞'),('feedLight','灯牌')]:
                value = sui['after'].get(kind)
                values.append(label + ('/'.join(map(str,value)) if value else '未知'))
            lines.append('岁己：' + '，'.join(values))
            gift = sui.get('gift')
            if gift == 'confirmed':
                lines.append('本轮确认补送1个灯牌（最多1电池）')
            elif gift in ('failed_or_uncertain_no_retry','sent_unconfirmed_no_retry'):
                lines.append('灯牌结果未确认，当天不重试以免重复扣费')
            elif str(uid) != str(primary_uid):
                lines.append('此账号仅免费，不送灯牌')
        failures = [r.get('reason', 'unknown') for r in rows if r.get('status') == 'error']
        if failures:
            lines.append('异常原因：' + '；'.join(dict.fromkeys(failures))[:160])
    lines += ['', '每4小时启动全量队列，岁己优先；其他主播仅免费，先遍历全部房间再补余下轮次。已满以B站实际进度为准。']
    return [(f'{day}-round-{round_id}', '\n'.join(lines))]


def report(settings, ledger, day, identities, results, round_id=None):
    return [{'event': key, 'status': send_notice(settings, ledger, key, text)}
            for key, text in events(day, identities, results, settings.get('PAID_ACCOUNT_UID'), round_id)]
