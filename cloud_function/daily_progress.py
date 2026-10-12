"""Daily roster and verified completions in an append-only OBS journal.

The gift reservation is deliberately separate and is never reset by this cache.
"""
import hashlib
import json
from room_priority import LIKE_POLICY_VERSION, LIKE_BASE_LIMIT, LIKE_PROBE_LIMIT


class ProgressError(RuntimeError):
    pass


def policy(account, settings):
    return {'uid': int(account['uid']), 'role': account.get('role'),
            'other_medals': account.get('other_medals') is True,
            'banned_uids': sorted({int(u) for u in account.get('banned_uids', [])}),
            'paid': (account.get('role') == 'primary' and account.get('allow_paid') is True
                     and str(account['uid']) == settings.get('PAID_ACCOUNT_UID')
                     and settings.get('ENABLE_PAID_GIFT') == 'true')}


def confirmed_task(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(n) is int for n in value)
            and 0 < value[1] <= 100 and value[0] == value[1])


def confirmed_complete(after, needs_lamp=False, needs_like=True):
    kinds = ('watchLive', 'sendDanmu') + (('like',) if needs_like else ()) + (('feedLight',) if needs_lamp else ())
    for kind in kinds:
        value = after.get(kind)
        if not confirmed_task(value):
            return False
    return True


class DailyProgress:
    def __init__(self, ledger, account, settings, day):
        self.ledger, self.day = ledger, day
        self.policy = policy(account, settings)
        serialized = json.dumps(self.policy, sort_keys=True, separators=(',', ':'))
        signature = hashlib.sha256(serialized.encode()).hexdigest()[:20]
        self.key = f'runs/daily/{self.policy["uid"]}/{day}-{signature}.jsonl'
        self.header, self.done, self.observed, self.position = None, {}, {}, 0
        self.like_targets, self.like_reserved_clicks = None, 0
        self.like_plan = None
        self.like_results, self.run_summaries = [], []
        self.last_like_attempt = None
        self.task_done = {kind: set() for kind in ('watchLive', 'sendDanmu', 'like')}
        try:
            raw = ledger.read_progress(self.key)
            if raw is None:
                return
            if not isinstance(raw, bytes) or not raw or not raw.endswith(b'\n'):
                raise ValueError('Incomplete journal')
            records = [json.loads(line) for line in raw.splitlines()]
            self.position = len(raw)
            if records[0] == {'type': 'init', 'schema': 1}:
                records = records[1:]
                if not records:
                    return
            header = records[0]
            if (header.get('schema') != 1 or header.get('type') != 'roster'
                    or header.get('day') != day or header.get('policy') != self.policy
                    or header.get('identity', {}).get('uid') != self.policy['uid']
                    or type(header.get('medal_rooms_total')) is not int):
                raise ValueError('Journal identity mismatch')
            targets = header['targets']
            if (not isinstance(targets, list) or len(targets) > 2000
                    or any(not isinstance(t, list) or len(t) != 2 or
                           any(type(n) is not int or n <= 0 for n in t) for t in targets)
                    or len({t[0] for t in targets}) != len(targets)):
                raise ValueError('Invalid roster')
            by_room = dict(targets)
            for row in records[1:]:
                if row.get('type') == 'like_plan':
                    selected = row.get('rooms')
                    if (self.like_targets is not None or not isinstance(selected, list)
                            or len(selected) > 16 or len(set(selected)) != len(selected)
                            or any(type(r) is not int or r not in by_room for r in selected)):
                        raise ValueError('Invalid like plan')
                    self.like_targets = selected
                    continue
                if row.get('type') == 'adaptive_like_plan':
                    self._validate_like_plan(row['plan'], by_room)
                    self.like_plan = row['plan']
                    self.like_targets = self.like_plan['rooms']
                    self._reopen_like_rooms()
                    continue
                if row.get('type') == 'like_attempt':
                    count = row.get('clicks')
                    if (self.like_targets is None or row.get('room') not in self.like_targets
                            or type(count) is not int or not 1 <= count <= 300
                            or self.like_reserved_clicks + count > self.like_limit):
                        raise ValueError('Invalid like reservation')
                    self.like_reserved_clicks += count
                    self.last_like_attempt = (row['room'], count, self.like_reserved_clicks)
                    continue
                if row.get('type') == 'like_result':
                    self._validate_like_result(row)
                    self.like_results.append(row)
                    continue
                if row.get('type') == 'run_summary':
                    if not isinstance(row.get('summary'), dict):
                        raise ValueError('Invalid run summary')
                    self.run_summaries.append(row['summary'])
                    continue
                room = row['room']
                if room not in by_room or row.get('type') not in ('complete', 'pending'):
                    raise ValueError('Invalid journal entry')
                self._remember_tasks(room, row.get('after', {}))
                if row['type'] == 'pending':
                    self.observed[room] = row
                    continue
                if not confirmed_complete(row.get('after', {}),
                                          by_room[room] == 1954091502 and self.policy['paid'],
                                          self.like_targets is None or room in self.like_targets):
                    raise ValueError('Invalid completion')
                self.done[room] = row['after']
            self.header, self.position = header, len(raw)
        except ProgressError:
            raise
        except Exception as exc:
            raise ProgressError('Daily progress could not be loaded: ' + type(exc).__name__) from None

    def _append(self, record):
        data = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        try:
            self.ledger.append_progress(self.key, data, self.position)
        except Exception as exc:
            # Uncertain outcomes stop this queue; the next load reconciles storage.
            raise ProgressError('Daily checkpoint failed; stop queue: ' + type(exc).__name__) from None
        self.position += len(data)

    def initialize(self, identity, targets, total):
        if self.header is not None:
            raise ProgressError('Roster already initialized')
        header = {'type': 'roster', 'schema': 1, 'day': self.day, 'policy': self.policy,
                  'identity': {'uid': int(identity['uid']), 'name': identity.get('name', '')},
                  'targets': [[int(r), int(u)] for r, u in targets], 'medal_rooms_total': int(total)}
        self._append(header)
        self.header = header

    def remaining(self):
        targets = [(r,u) for r,u in self.header['targets'] if r not in self.done]
        # Never starve the unvisited tail behind the same incomplete rooms.
        return sorted(targets, key=lambda t: 0 if t[1] == 1954091502 else (2 if t[0] in self.observed else 1))

    def set_like_plan(self, rooms):
        if self.like_targets is not None:
            return
        roster = {r for r, _ in self.header['targets']}
        if (len(rooms) > 16 or len(set(rooms)) != len(rooms)
                or any(type(r) is not int or r not in roster for r in rooms)):
            raise ProgressError('Invalid like plan')
        self._append({'type': 'like_plan', 'rooms': list(rooms)})
        self.like_targets = list(rooms)

    @property
    def like_limit(self):
        return LIKE_PROBE_LIMIT if self.like_plan else LIKE_BASE_LIMIT

    def _validate_like_plan(self, plan, roster):
        rooms, levels = plan.get('rooms'), plan.get('levels')
        threshold = plan.get('threshold')
        if (plan.get('version') != LIKE_POLICY_VERSION or not isinstance(rooms, list)
                or len(rooms) > 2000 or len(set(rooms)) != len(rooms)
                or any(type(r) is not int or r not in roster for r in rooms)
                or not isinstance(levels, list)
                or any(not isinstance(pair, list) or len(pair) != 2
                       or type(pair[0]) is not int or pair[0] not in roster
                       or type(pair[1]) is not int or not 1 <= pair[1] <= 120 for pair in levels)
                or len({pair[0] for pair in levels}) != len(levels)
                or (threshold is not None and (type(threshold) is not int or not 1 <= threshold <= 120))
                or type(plan.get('minimum_rooms')) is not int or not 1 <= plan['minimum_rooms'] <= 2000
                or type(plan.get('verified_rooms')) is not int or not 0 <= plan['verified_rooms'] <= len(roster)):
            raise ProgressError('Invalid adaptive like plan')
        by_level = dict(levels)
        expected = sorted(by_level, key=lambda room: (by_level[room], room))
        priority = 25788785
        expected = ([priority] if priority in roster else []) + [r for r in expected if r != priority]
        if rooms != expected or (threshold is None and levels) or any(level > threshold for level in by_level.values()):
            raise ProgressError('Inconsistent adaptive like plan')

    def _reopen_like_rooms(self):
        # A previously watch/chat-complete room may become eligible for likes.
        # Preserve its verified counters, but resume its newly required task.
        for room, after in list(self.done.items()):
            if room in self.like_targets and not confirmed_task(after.get('like')):
                self.observed[room] = {'type': 'pending', 'room': room, 'after': after,
                                       'error': False, 'storage_full': False}
                del self.done[room]

    def update_like_plan(self, plan):
        self._validate_like_plan(plan, dict(self.header['targets']))
        if self.like_plan == plan:
            return
        self._append({'type': 'adaptive_like_plan', 'plan': plan})
        self.like_plan = plan
        self.like_targets = list(plan['rooms'])
        self._reopen_like_rooms()

    def _validate_like_result(self, row):
        if (self.last_like_attempt != (row.get('room'), row.get('clicks'), row.get('reserved_total'))
                or row.get('accepted') is not True
                or any(value is not None and (not isinstance(value, list) or len(value) != 2
                        or any(type(n) is not int for n in value) or not 0 <= value[0] <= value[1] <= 100)
                       for value in (row.get('before'), row.get('after')))
                or (self.like_results and self.like_results[-1]['reserved_total'] == row['reserved_total'])):
            raise ProgressError('Invalid like result')

    def record_like_result(self, room, clicks, before, after):
        row = {'type': 'like_result', 'room': room, 'clicks': clicks,
               'reserved_total': self.like_reserved_clicks, 'accepted': True,
               'before': before, 'after': after}
        self._validate_like_result(row)
        self._append(row)
        self.like_results.append(row)

    def like_probe_summary(self):
        batches = [row for row in self.like_results
                   if row['reserved_total'] - row['clicks'] >= LIKE_BASE_LIMIT]
        return {'reserved_clicks': max(0, self.like_reserved_clicks - LIKE_BASE_LIMIT),
                'accepted_batches': len(batches),
                'counter_increased_batches': sum(bool(row['before'] and row['after']
                    and row['before'][1] == row['after'][1] and row['after'][0] > row['before'][0])
                                                for row in batches)}

    def record_run_summary(self, summary):
        self._append({'type': 'run_summary', 'summary': summary})
        self.run_summaries.append(summary)

    def reserve_like(self, room, clicks):
        """Count attempts before POST; lost responses never replenish the budget."""
        if self.like_targets is None or room not in self.like_targets:
            return False
        if type(clicks) is not int or not 1 <= clicks <= 300:
            raise ProgressError('Invalid like count')
        if self.like_reserved_clicks + clicks > self.like_limit:
            return False
        self._append({'type': 'like_attempt', 'room': room, 'clicks': clicks})
        self.like_reserved_clicks += clicks
        self.last_like_attempt = (room, clicks, self.like_reserved_clicks)
        return True

    def _remember_tasks(self, room, after):
        for kind, rooms in self.task_done.items():
            if confirmed_task(after.get(kind)):
                rooms.add(room)

    def task_counts(self):
        return {kind: len(rooms) for kind, rooms in self.task_done.items()}

    def record(self, room, row, current_day):
        if current_day != self.day or room in self.done:
            return False
        by_room = dict(self.header['targets'])
        if room not in by_room:
            raise ProgressError('Room absent from roster')
        after = {k: row.get('after', {}).get(k) for k in ('watchLive', 'sendDanmu', 'like', 'feedLight')}
        if row.get('status') == 'error' or not confirmed_complete(
                after, by_room[room] == 1954091502 and self.policy['paid'],
                self.like_targets is None or room in self.like_targets):
            entry = {'type': 'pending', 'room': room, 'after': after,
                     'error': row.get('status') == 'error', 'storage_full': bool(row.get('storage_full'))}
            if self.observed.get(room) != entry:
                self._append(entry)
                self.observed[room] = entry
            self._remember_tasks(room, after)
            return False
        self._append({'type': 'complete', 'room': room, 'after': after})
        self.done[room] = after.copy()
        self._remember_tasks(room, after)
        return True
