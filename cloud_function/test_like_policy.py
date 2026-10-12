import io
import json
import time
import unittest
from unittest.mock import Mock, patch

import index as cloud
from daily_progress import DailyProgress, ProgressError
from room_priority import select_like_targets
from test_cloud import Ledger


class LikePolicyTests(unittest.TestCase):
    def state(self):
        state = DailyProgress(Ledger(), {'uid':123}, {}, cloud.today())
        state.initialize({'uid':123}, [(cloud.SUI_ROOM, cloud.SUI_UID), (1,11), (2,22)], 3)
        return state

    def test_priority_plus_lowest_fifteen_ignores_unknown_levels(self):
        targets = [(r,100+r) for r in range(1,20)] + [(cloud.SUI_ROOM,cloud.SUI_UID)]
        levels = {r: 20-r for r in range(1,20)}
        self.assertEqual(select_like_targets(targets, levels), [cloud.SUI_ROOM] + list(range(19,4,-1)))
        self.assertEqual(select_like_targets(targets, {1:None, 2:'1', 3:False, 4:1, 5:1}),
                         [cloud.SUI_ROOM,4,5])

    def test_old_progress_and_plan_survive_restart_without_policy_reset(self):
        state = self.state()
        state.record(1, {'after':{'sendDanmu':[10,10]}}, cloud.today())
        key = state.key
        state.set_like_plan([cloud.SUI_ROOM,2])
        self.assertTrue(state.reserve_like(2,300))
        restored = DailyProgress(state.ledger, {'uid':123}, {}, cloud.today())
        self.assertEqual(restored.key,key)
        self.assertEqual(restored.like_targets,[cloud.SUI_ROOM,2])
        self.assertEqual(restored.like_reserved_clicks,300)
        self.assertEqual(restored.task_counts()['sendDanmu'],1)
        restored.set_like_plan([1])
        self.assertEqual(restored.like_targets,[cloud.SUI_ROOM,2])

    def test_daily_attempt_budget_is_durable_and_excludes_other_rooms(self):
        state = self.state()
        state.set_like_plan([1])
        for _ in range(16):
            self.assertTrue(state.reserve_like(1,300))
        self.assertTrue(state.reserve_like(1,200))
        self.assertFalse(state.reserve_like(1,30))
        self.assertFalse(state.reserve_like(2,30))
        restored = DailyProgress(state.ledger, {'uid':123}, {}, cloud.today())
        self.assertEqual(restored.like_reserved_clicks,5000)
        self.assertFalse(restored.reserve_like(1,1))

    def test_uncertain_reservation_never_authorizes_post(self):
        state = self.state()
        state.set_like_plan([1])
        original = state.ledger.append_progress
        def uncertain(*args):
            original(*args)
            raise TimeoutError()
        state.ledger.append_progress = uncertain
        client = cloud.Bili('SESSDATA=example; bili_jct=example',123)
        client.like_budget = state.reserve_like
        client.request = Mock()
        with self.assertRaises(ProgressError):
            client.like(1,11,30)
        client.request.assert_not_called()
        restored = DailyProgress(state.ledger, {'uid':123}, {}, cloud.today())
        self.assertEqual(restored.like_reserved_clicks,30)

    def test_outside_selection_still_completes_offline_chat(self):
        client = Mock()
        client.gate = cloud.AccountGate()
        client.room.return_value = {'uid':11,'live_status':0}
        client.danmu.return_value = True
        data = {'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 0/1'},
                             {'jump_type':'like','title':'点赞30次','sub_title':'每日上限 0/10'}]}
        done = {'task_info':[{'jump_type':'sendDanmu','sub_title':'每日上限 1/1'}]}
        client.tasks.return_value = done
        audit = {}
        with patch('index.time.sleep'):
            cloud.free_actions(client,1,11,data,cloud.today(),time.monotonic()+60,10,audit,allow_like=False)
        client.like.assert_not_called()
        client.danmu.assert_called_once()
        self.assertEqual(audit['like']['stop'],'outside_like_selection')

    def test_unselected_room_completion_does_not_require_like_credit(self):
        state = self.state()
        state.set_like_plan([cloud.SUI_ROOM,2])
        row = {'after':{'watchLive':[10,10],'sendDanmu':[10,10],'like':[0,10]}}
        self.assertTrue(state.record(1,row,cloud.today()))
        self.assertFalse(state.record(2,row,cloud.today()))
        restored = DailyProgress(state.ledger, {'uid':123}, {}, cloud.today())
        self.assertIn(1,restored.done)
        self.assertNotIn(2,restored.done)
        self.assertEqual(restored.task_counts()['like'],0)

    def test_batch_submission_handles_delayed_credit_without_unbounded_sweeps(self):
        client = Mock()
        client.gate = cloud.AccountGate()
        client.room.return_value = {'uid':11,'live_status':1}
        def data(n):
            return {'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':f'每日上限 {n}/10'}]}
        client.tasks.side_effect = [data(5),data(5),data(5),data(5),data(10)]
        audit = {}
        with patch('index.time.sleep'):
            result = cloud.free_actions(client,1,11,data(5),cloud.today(),time.monotonic()+60,10,audit)
        self.assertEqual(client.like.call_count,5)
        self.assertEqual(cloud.progress(result,'like')[:2],(10,10))
        self.assertEqual(audit['like']['stop'],'full')

    def test_panel_preserves_levels_without_changing_room_map_contract(self):
        client = cloud.Bili('SESSDATA=example; bili_jct=example',123)
        payload = {'code':0,'data':{'list':[{'medal':{'target_id':11,'level':3},'room_info':{'room_id':1}}]}}
        client.opener.open = Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
        self.assertEqual(client.medal_rooms(),{1:11})
        self.assertEqual(client.medal_levels,{1:3})

    def test_changed_task_schema_stops_further_like_batches_and_sweeps(self):
        client = Mock()
        client.gate = cloud.AccountGate()
        client.room.return_value = {'uid':11,'live_status':1}
        initial = {'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':'每日上限 0/10'}]}
        changed = {'task_info':[{'jump_type':'like','title':'点赞60次','sub_title':'每日上限 0/10'}]}
        client.tasks.return_value = changed
        audit = {}
        with patch('index.time.sleep'):
            result = cloud.free_actions(client,1,11,initial,cloud.today(),time.monotonic()+60,10,audit)
            cloud.free_actions(client,1,11,result,cloud.today(),time.monotonic()+60,10,audit)
        client.like.assert_called_once_with(1,11,30)
        self.assertEqual(audit['like']['stop'],'task_changed')

    def test_initial_batch_limit_is_not_resent_in_later_sweep(self):
        client = Mock()
        client.gate = cloud.AccountGate()
        client.room.return_value = {'uid':11,'live_status':1}
        def data(n):
            return {'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':f'每日上限 {n}/10'}]}
        client.tasks.side_effect = [data(5),data(5),data(5),data(5),data(6)]
        audit = {}
        with patch('index.time.sleep'):
            result = cloud.free_actions(client,1,11,data(5),cloud.today(),time.monotonic()+60,10,audit)
            cloud.free_actions(client,1,11,result,cloud.today(),time.monotonic()+60,10,audit)
        self.assertEqual(client.like.call_count,5)
        self.assertEqual(audit['like']['stop'],'planned_batches_sent')

    def test_queue_upgrades_fixed_plan_and_prioritizes_verified_low_levels(self):
        state = self.state()
        state.set_like_plan([cloud.SUI_ROOM,2])
        client = Mock(uid=123,watch_seconds=0)
        client.gate = cloud.AccountGate()
        client.login.return_value = {'uid':123}
        client.tasks.return_value = {'task_info':[]}
        client.live_statuses.return_value = {1:1,2:1}
        client.room.return_value = {'live_status':0}
        client.medal_levels = {1:20, 2:1}
        calls = []
        def free(*args, **kwargs):
            calls.append((args[1],kwargs['allow_like']))
            return args[3]
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='disabled'), \
                patch('index.free_actions',side_effect=free):
            meta, rows = cloud.run_account_queue({'uid':123,'cookie':'unused'},cloud.today(),time.monotonic()+60,state.ledger,{})
        client.medal_rooms.assert_not_called()
        self.assertEqual(calls,[(cloud.SUI_ROOM,True),(2,True),(1,True)])
        self.assertEqual(meta['like_target_rooms'],[cloud.SUI_ROOM,2,1])
        self.assertEqual(meta['like_level_threshold'],20)
