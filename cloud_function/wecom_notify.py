"""Small WeCom notifier with durable once-per-event delivery attempts."""
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from run_schedule import INTERVAL_SECONDS
from daily_progress import confirmed_complete, confirmed_task
from room_priority import LIKE_BASE_LIMIT, LIKE_PROBE_LIMIT, LIKE_MIN_ROOMS

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


def events(day, identities, results, primary_uid, round_id=None, sui_only=False):
    if not results and not any(r.get('status') == 'error' for r in identities):
        return []
    round_id = str(round_id if round_id is not None else int(time.time()) // INTERVAL_SECONDS)
    accounts = {str(r.get('account', {}).get('uid')): r for r in identities}
    for row in results:
        account = row.get('account') or {'uid': row.get('uid', 'unknown')}
        accounts.setdefault(str(account.get('uid')), {'account': account})
    rooms = {r['room'] for r in results if r.get('room')}
    lines = ['直播亲密度任务 · 本轮统计', day,
             f'本轮检查：{len(results)}次，涉及{len(rooms)}个直播间（{len(accounts)}个账号分别统计）']
    for uid, identity in accounts.items():
        name = identity.get('account', {}).get('name', uid)
        rows = [r for r in results if str((r.get('account') or {}).get('uid', r.get('uid', 'unknown'))) == uid]
        if identity.get('status') == 'error':
            lines += ['', str(name) + '：账号检查失败', str(identity.get('reason', 'unknown'))[:120]]
            continue
        full, improved, storage, duplicate, errors, seconds = 0, 0, 0, 0, 0, 0
        relight_only, relighted = 0, 0
        task_full = {k:0 for k in ('watchLive','sendDanmu','like')}
        for row in rows:
            after, before = row.get('after', {}), row.get('before', {})
            if confirmed_complete(after):
                full += 1
            for kind in task_full:
                if confirmed_task(after.get(kind)):
                    task_full[kind] += 1
            relight_only += bool(row.get('relight_only'))
            relighted += row.get('before_lighted') is False and row.get('after_lighted') is True
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
                  f'本轮确认免费日任务已满{full}间；本轮有进展{improved}间',
                  f'储蓄满跳过{storage}间；重复跳过{duplicate}间；异常{errors}间',
                  f'已接受观看心跳：{seconds}秒']
        lines += [f'本轮各项已满：观看{task_full["watchLive"]}间；弹幕{task_full["sendDanmu"]}间；点赞{task_full["like"]}间',
                  f'仅点亮任务{relight_only}间；本轮确认点亮{relighted}间']
        cumulative = identity.get('daily_task_completed_rooms')
        if cumulative is not None:
            lines.append(f'今日累计单项已满：点赞{cumulative.get("like",0)}间；弹幕{cumulative.get("sendDanmu",0)}间；观看{cumulative.get("watchLive",0)}间')
        if 'like_target_rooms' in identity:
            threshold = identity.get('like_level_threshold')
            label = f'≤{threshold}级' if threshold is not None else '等级未知，仅优先房间'
            lines.append(f'点赞范围：全部牌子最低至少{LIKE_MIN_ROOMS}间确定门槛{label}，同级一起纳入；共{len(identity["like_target_rooms"])}间，仅开播执行')
            limit = identity.get('like_attempted_click_limit', LIKE_PROBE_LIMIT)
            lines.append(f'今日点赞预约{identity.get("like_reserved_clicks",0)}/{limit}次；{LIKE_BASE_LIMIT}以上为实验，非官方额度（含失败/不确定请求）')
            if identity.get('like_priority_reserved_clicks'):
                lines.append(f'优先房间预留{identity["like_priority_reserved_clicks"]}次基础预算，确认当天点赞已满后释放')
            probe = identity.get('like_probe') or {}
            if probe.get('reserved_clicks'):
                lines.append(f'超5000实验：预约{probe["reserved_clicks"]}次；完整实验批接受{probe.get("accepted_batches",0)}批，计数增长{probe.get("counter_increased_batches",0)}批（不等于额度已验证）')
        if any('actions' in row for row in rows):
            counts = {kind: sum(row.get('actions',{}).get(kind,{}).get('accepted',0)
                               for row in rows) for kind in ('like','sendDanmu')}
            lines.append(f'本轮接口接受：点赞{counts["like"]}批；弹幕{counts["sendDanmu"]}条（含点亮，不等于亲密度入账）')
            stalled = {kind: sum(row.get('actions',{}).get(kind,{}).get('stop') == 'no_confirmed_progress'
                                for row in rows) for kind in ('like','sendDanmu')}
            if any(stalled.values()):
                lines.append(f'计数未增长而停止：点赞{stalled["like"]}间；弹幕{stalled["sendDanmu"]}间')
        if 'live_priority_rooms' in identity:
            lines.append(f'排序时其他开播房间{identity["live_priority_rooms"]}间；未播或轮播不观看、不点赞')
        lines.append('最多8间并行观看；每间每轮最多16分钟，岁己同样轮换；心跳成功不等于任务已满')
        if 'cached_completed_rooms' in identity:
            lines.append(f'今日累计策略任务完成{identity.get("daily_completed_rooms",0)}间；已完成直接跳过{identity["cached_completed_rooms"]}间；剩余队列{identity.get("remaining_rooms",0)}间')
        lines.append(f'本轮尚未检查{identity.get("pending_rooms",0)}间；本轮已检查但三项未满{identity.get("incomplete_rooms",0)}间')
        if identity.get('paused_by_risk'):
            lines.append('账号触发风控，本次队列已暂停，未继续重试。')
        if identity.get('danmaku_frequency_rejections'):
            paused = '；本轮弹幕已暂停' if identity.get('danmaku_paused') else ''
            lines.append(f'弹幕小频控{identity["danmaku_frequency_rejections"]}次；最多等待60秒、120秒各续发一次{paused}')
        diagnostics = identity.get('request_diagnostics') or {}
        if diagnostics and identity.get('paused_by_risk'):
            counts = diagnostics.get('request_counts', {})
            likes = counts.get('/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3', 0)
            lines.append(f'接口诊断 like-risk-v1：请求尝试{sum(counts.values())}次；点赞请求{likes}次')
            rejection = diagnostics.get('last_rejection') or {}
            if rejection:
                lines.append(f'拒绝前请求间隔{rejection.get("previous_request_gap_seconds")}秒；'
                             f'本轮经过{rejection.get("elapsed_seconds")}秒；'
                             f'验证码标记{"有" if rejection.get("challenge_present") else "无"}')
                fields = rejection.get('cookie_fields_present', {})
                lines.append('设备凭据：' + '，'.join(f'{key}{"有" if present else "无"}'
                                                  for key, present in fields.items()))
                lines.append('各接口请求尝试：' + '；'.join(f'{path.rsplit("/",1)[-1]}={count}'
                                                         for path, count in counts.items()))
            lines.append('登录凭据维护：' + str(identity.get('credential_refresh', 'unknown')))
        sui = next((r for r in rows if r.get('room') == 25788785 and 'after' in r), None)
        if sui:
            values = []
            for kind, label in [('watchLive','观看'),('sendDanmu','弹幕'),('like','点赞'),('feedLight','灯牌')]:
                value = sui['after'].get(kind)
                values.append(label + ('/'.join(map(str,value)) if value else '未知'))
            lines.append('岁己：' + '，'.join(values))
            stops = {'not_live':'未开播', 'storage_full':'储蓄满',
                     'daily_like_budget':'今日点赞预约额度用尽', 'planned_batches_sent':'计划点赞批次已提交，待确认入账',
                     'no_confirmed_progress':'请求已接受但计数未增长',
                     'unknown_task':'任务计数未知', 'deadline':'本轮时间用尽',
                     'endpoint_paused':'接口已暂停', 'round_budget':'本次次数用尽',
                     'relight_unconfirmed':'点亮未确认', 'unknown_round_count':'次数要求未知'}
            stops['task_changed'] = '任务格式变化，停止继续提交'
            stop = sui.get('actions',{}).get('like',{}).get('stop')
            if stop in stops:
                lines.append('岁己点赞暂未补满：' + stops[stop])
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
    scope = '当前仅运行已启用账号的岁己直播间；其他主播任务已关闭。' if sui_only else '岁己第一，随后开播点赞和观看，最后未播弹幕；其他主播仅免费。'
    completion = ('策略任务完成：名单内需观看、弹幕、点赞满；名单外仅需观看、弹幕满；付费目标另需灯牌满。'
                  if any('like_target_rooms' in identity for identity in identities) else '“累计完成”须三项均满；')
    lines += ['', completion + '单项累计按当天接口确认去重，未知/仅点亮不计入。开播期间不发弹幕，未播不观看/点赞。', '每2小时续跑当天剩余队列；每轮最多110分钟，单实例单请求运行；已完成当日不重查，跨天重置。' + scope]
    chunks = notice_chunks('\n'.join(lines))
    key = f'{day}-round-{round_id}'
    return [(key if i == 0 else key + f'-part-{i+1}', chunk) for i, chunk in enumerate(chunks)]


def notice_chunks(text, limit=2000):
    """Keep both account summaries; never silently truncate the second account."""
    chunks, current = [], ''
    for line in text.splitlines(keepends=True):
        while len(line.encode('utf-8')) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ''
            part = line.encode('utf-8')[:limit].decode('utf-8', errors='ignore')
            chunks.append(part)
            line = line[len(part):]
        if len((current + line).encode('utf-8')) > limit:
            chunks.append(current.rstrip())
            current = ''
        current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


def report(settings, ledger, day, identities, results, round_id=None):
    return [{'event': key, 'status': send_notice(settings, ledger, key, text)}
            for key, text in events(day, identities, results, settings.get('PAID_ACCOUNT_UID'), round_id,
                                    settings.get('SUI_ONLY', '').strip().lower() == 'true')]
