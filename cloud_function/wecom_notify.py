"""Small WeCom notifier with durable once-per-event delivery attempts."""
import hashlib
import json
import logging
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


def events(day, identities, results, primary_uid):
    notices = []
    for row in identities:
        if row.get('status') == 'error':
            uid = row.get('account', {}).get('uid', 'unknown')
            notices.append((f'{day}-account-{uid}', f'岁己云函数：账号 {uid} 登录或任务读取失败。\n{day}\n{row.get("reason", "unknown")}\n其他账号会继续执行。'))
    for row in results:
        if row.get('status') == 'error':
            reason = row.get('reason', 'unknown')
            code = hashlib.sha256(reason.encode()).hexdigest()[:12]
            notices.append((f'{day}-error-{code}', f'岁己云函数执行异常\n{day}\n{reason}\n请查看华为云函数执行记录；相同错误当天只提醒一次。'))
            continue
        if row.get('room') != 25788785 or 'after' not in row:
            continue
        account = row.get('account', {})
        uid, name = account.get('uid'), account.get('name', str(account.get('uid')))
        state = row['after']
        lines = [f'岁己亲密度任务 · {name}', day]
        for kind, label in [('watchLive','观看'),('sendDanmu','弹幕'),('like','点赞'),('feedLight','灯牌')]:
            value = state.get(kind)
            lines.append(label + '：' + ('/'.join(map(str,value)) if value else '状态未知'))
        if row.get('gift') == 'confirmed':
            notices.append((f'{day}-gift-{uid}', '\n'.join(lines + ['已确认补送 1 个粉丝团灯牌，最多消耗 1 电池。'])))
        elif row.get('gift') in ('failed_or_uncertain_no_retry', 'sent_unconfirmed_no_retry'):
            notices.append((f'{day}-gift-uncertain-{uid}', '\n'.join(lines + ['灯牌结果未确认，当天不再重试，避免重复扣费。'])))
        if row.get('storage_full'):
            notices.append((f'{day}-storage-{uid}', '\n'.join(lines + ['免费亲密度储蓄已满，本轮未继续免费任务。副账号不会自动购买灯牌。'])))
        free_complete = all(state.get(k) and state[k][0] >= state[k][1] for k in ('watchLive','sendDanmu','like'))
        lamp = state.get('feedLight')
        if free_complete and (str(uid) != str(primary_uid) or (lamp and lamp[0] >= lamp[1])):
            notices.append((f'{day}-complete-{uid}', '\n'.join(lines + ['今天的目标任务已完成。'])))
    return notices


def report(settings, ledger, day, identities, results):
    return [{'event': key, 'status': send_notice(settings, ledger, key, text)}
            for key, text in events(day, identities, results, settings.get('PAID_ACCOUNT_UID'))]
