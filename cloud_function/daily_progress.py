"""Daily roster and verified completions in an append-only OBS journal.

The gift reservation is deliberately separate and is never reset by this cache.
"""
import hashlib
import json


class ProgressError(RuntimeError):
    pass


def policy(account, settings):
    return {'uid': int(account['uid']), 'role': account.get('role'),
            'other_medals': account.get('other_medals') is True,
            'banned_uids': sorted({int(u) for u in account.get('banned_uids', [])}),
            'paid': (account.get('role') == 'primary' and account.get('allow_paid') is True
                     and str(account['uid']) == settings.get('PAID_ACCOUNT_UID')
                     and settings.get('ENABLE_PAID_GIFT') == 'true')}


def confirmed_complete(after, needs_lamp=False):
    kinds = ('watchLive', 'sendDanmu', 'like') + (('feedLight',) if needs_lamp else ())
    for kind in kinds:
        value = after.get(kind)
        if (not isinstance(value, list) or len(value) != 2
                or any(type(n) is not int for n in value)
                or not 0 < value[1] <= 100 or value[0] != value[1]):
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
        try:
            raw = ledger.read_progress(self.key)
            if raw is None:
                return
            if not isinstance(raw, bytes) or not raw or not raw.endswith(b'\n'):
                raise ValueError('Incomplete journal')
            records = [json.loads(line) for line in raw.splitlines()]
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
                room = row['room']
                if room not in by_room or row.get('type') not in ('complete', 'pending'):
                    raise ValueError('Invalid journal entry')
                if row['type'] == 'pending':
                    self.observed[room] = row
                    continue
                if not confirmed_complete(row.get('after', {}),
                                          by_room[room] == 1954091502 and self.policy['paid']):
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

    def record(self, room, row, current_day):
        if current_day != self.day or room in self.done:
            return False
        by_room = dict(self.header['targets'])
        if room not in by_room:
            raise ProgressError('Room absent from roster')
        after = {k: row.get('after', {}).get(k) for k in ('watchLive', 'sendDanmu', 'like', 'feedLight')}
        if row.get('status') == 'error' or not confirmed_complete(after, by_room[room] == 1954091502 and self.policy['paid']):
            entry = {'type': 'pending', 'room': room, 'after': after,
                     'error': row.get('status') == 'error', 'storage_full': bool(row.get('storage_full'))}
            if self.observed.get(room) != entry:
                self._append(entry)
                self.observed[room] = entry
            return False
        self._append({'type': 'complete', 'room': room, 'after': after})
        self.done[room] = after.copy()
        return True
