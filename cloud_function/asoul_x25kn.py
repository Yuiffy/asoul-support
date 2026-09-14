"""X25Kn protocol adapted from XiaoYiWeio/asoul-support v4.1.1.

Upstream commit b52664ccc8389992acb8335422a760e4d8cf1c66, scripts/heartbeat.py.
The transport is injected; account configuration, scheduling and notifications
are intentionally outside this module. See THIRD_PARTY.md.
"""
import hashlib
import hmac
import json
import time

ALGORITHMS = ['md5', 'sha1', 'sha256', 'sha224', 'sha512', 'sha384']


def sign(payload_json, rules, secret_key):
    result = payload_json
    for rule in rules:
        if rule not in range(len(ALGORITHMS)):
            raise ValueError('Unsupported X25Kn HMAC rule')
        result = hmac.new(secret_key.encode(), result.encode(),
                          getattr(hashlib, ALGORITHMS[rule])).hexdigest()
    return result


def packed(value):
    return json.dumps(value, separators=(',', ':'))


def enter(transport, ids, device, uid):
    return transport('E', {'id': packed(ids), 'device': packed(device),
                           'ruid': uid, 'ts': int(time.time() * 1000),
                           'is_patch': 0, 'heart_beat': '[]'})


def heartbeat(transport, ids, device, uid, state):
    timestamp = int(time.time() * 1000)
    payload = {'platform': 'web', 'parent_id': ids[0], 'area_id': ids[1],
               'seq_id': ids[2], 'room_id': ids[3], 'buvid': device[0],
               'uuid': device[1], 'ets': state['timestamp'],
               'time': state['heartbeat_interval'], 'ts': timestamp}
    signature = sign(packed(payload), state['secret_rule'], state['secret_key'])
    return transport('X', {'s': signature, 'id': packed(ids), 'device': packed(device),
                           'ruid': uid, 'ets': state['timestamp'],
                           'benchmark': state['secret_key'],
                           'time': state['heartbeat_interval'], 'ts': timestamp,
                           'trackid': '-99998'})
