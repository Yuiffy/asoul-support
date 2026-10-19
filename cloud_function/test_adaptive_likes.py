import json
import time
import unittest
from unittest.mock import Mock, patch

import index as cloud
from daily_progress import DailyProgress, ProgressError
from room_priority import adaptive_like_plan
from test_cloud import Ledger
from wecom_notify import events


class AdaptiveLikeTests(unittest.TestCase):
    def state(self, ledger=None, uid=123):
        account = {'uid': uid, 'role': 'secondary'}
        state = DailyProgress(ledger or Ledger(), account, {}, cloud.today())
        if not state.header:
            state.initialize({'uid': uid}, [(cloud.SUI_ROOM, cloud.SUI_UID), (1, 11), (2, 22)], 3)
        return state

    def test_threshold_uses_all_medals_and_includes_boundary_ties(self):
        targets = [(r, 1000+r) for r in range(1, 151)] + [(cloud.SUI_ROOM, cloud.SUI_UID)]
        levels = {r: (1 if r < 100 else 2 if r <= 110 else 3) for r in range(1, 151)}
        plan = adaptive_like_plan(targets, levels)
        self.assertEqual(plan['threshold'], 2)
        self.assertEqual(plan['rooms'], [cloud.SUI_ROOM] + list(range(1, 111)))
        self.assertEqual(plan['verified_rooms'], 150)
        self.assertEqual(plan['minimum_rooms'], 100)

    def test_fewer_than_minimum_uses_all_verified_levels_and_priority_exception(self):
        targets = [(1,11), (2,22), (3,33), (4,44), (cloud.SUI_ROOM, cloud.SUI_UID)]
        plan = adaptive_like_plan(targets, {1:None, 2:'1', 3:False, 4:5})
        self.assertEqual(plan['rooms'], [cloud.SUI_ROOM, 4])
        self.assertEqual(plan['threshold'], 5)

    def test_widen_old_fifty_plan_preserves_budget_and_reopens_new_target(self):
        account = {'uid':123, 'role':'secondary'}
        state = DailyProgress(Ledger(), account, {}, cloud.today())
        targets = [(r, 1000+r) for r in range(1, 121)]
        state.initialize({'uid':123}, targets, len(targets))
        levels = {r:r for r in range(1, 121)}
        old_plan = adaptive_like_plan(targets, levels, minimum=50)
        old_plan['version'] = 'adaptive_lowest50_v2'
        state.update_like_plan(old_plan)
        state.reserve_like(1,300)
        state.record(75, {'after':{'watchLive':[10,10], 'sendDanmu':[10,10],
                                 'like':[0,10]}}, cloud.today())
        restored = DailyProgress(state.ledger, account, {}, cloud.today())
        self.assertIn(75, restored.done)
        restored.update_like_plan(adaptive_like_plan(targets, levels))
        reloaded = DailyProgress(state.ledger, account, {}, cloud.today())
        self.assertEqual(reloaded.key, state.key)
        self.assertEqual(reloaded.like_reserved_clicks,300)
        self.assertEqual(len(reloaded.like_targets),100)
        self.assertNotIn(75,reloaded.done)
        self.assertIn((75,1075),reloaded.remaining())
        self.assertEqual(reloaded.task_counts()['sendDanmu'],1)

    def test_midday_upgrade_preserves_five_thousand_then_limits_probe_to_six_thousand(self):
        state = self.state()
        state.set_like_plan([1])
        for _ in range(16):
            self.assertTrue(state.reserve_like(1,300))
        self.assertTrue(state.reserve_like(1,200))
        old_key = state.key
        state.record(cloud.SUI_ROOM, {'after':{'like':[10,10]}},cloud.today())
        state.update_like_plan(adaptive_like_plan(state.header['targets'], {1:1,2:2}))
        for _ in range(3):
            self.assertTrue(state.reserve_like(2,300))
        self.assertTrue(state.reserve_like(2,100))
        self.assertFalse(state.reserve_like(2,1))
        restored = self.state(state.ledger)
        self.assertEqual(restored.key, old_key)
        self.assertEqual(restored.like_reserved_clicks,6000)
        self.assertEqual(restored.like_probe_summary()['reserved_clicks'],1000)
        self.assertFalse(restored.reserve_like(1,30))

    def test_adaptive_plan_refresh_never_resets_attempts_and_reopens_new_like_task(self):
        state = self.state()
        state.set_like_plan([cloud.SUI_ROOM])
        row = {'after':{'watchLive':[10,10],'sendDanmu':[10,10],'like':[0,10]}}
        state.record(1,row,cloud.today())
        self.assertIn(1,state.done)
        state.reserve_like(cloud.SUI_ROOM,30)
        state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        restored = self.state(state.ledger)
        self.assertNotIn(1,restored.done)
        self.assertIn((1,11),restored.remaining())
        self.assertEqual(restored.like_reserved_clicks,30)
        self.assertEqual(restored.observed[1]['after'],dict(row['after'], feedLight=None))

    def test_budget_is_independent_for_two_accounts(self):
        first = self.state()
        second = self.state(first.ledger,456)
        for state in (first,second):
            state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        first.reserve_like(1,300)
        self.assertEqual(self.state(first.ledger,456).like_reserved_clicks,0)

    def test_secondary_paid_flag_never_enables_paid_gifts_in_health(self):
        settings = {'ENABLE_PAID_GIFT':'true','PAID_ACCOUNT_UID':'456',
            'BILIBILI_ACCOUNTS_JSON':json.dumps([
                {'uid':123,'role':'primary','allow_paid':True},
                {'uid':456,'role':'secondary','allow_paid':True}])}
        with patch('index.settings_from',return_value=settings):
            health = cloud.handler({'mode':'health'},None)
        self.assertFalse(health['scope']['accounts'][1]['paid_gift_allowed'])
        self.assertEqual(health['like_policy']['minimum_threshold_rooms'],100)
        self.assertEqual(health['like_policy']['daily_attempted_click_limit'],6000)

    def test_probe_acceptance_is_separate_from_credited_counter(self):
        state = self.state()
        state.record(cloud.SUI_ROOM, {'after':{'like':[10,10]}},cloud.today())
        state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        for _ in range(16):
            state.reserve_like(1,300)
        state.reserve_like(1,200)
        state.reserve_like(1,30)
        state.record_like_result(1,30,[0,10],[0,10])
        state.reserve_like(1,30)
        state.record_like_result(1,30,[0,10],[1,10])
        restored = self.state(state.ledger)
        self.assertEqual(restored.like_probe_summary(),{'reserved_clicks':60,
            'accepted_batches':2,'counter_increased_batches':1})
        with self.assertRaises(ProgressError):
            restored.record_like_result(1,30,[0,10],[1,10])

    def test_priority_reserve_blocks_other_rooms_and_probe_until_verified_full(self):
        state = self.state()
        state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        for _ in range(15):
            self.assertTrue(state.reserve_like(1,300))
        self.assertTrue(state.reserve_like(1,170))
        restored = self.state(state.ledger)
        self.assertEqual(restored.priority_like_reserve,330)
        self.assertFalse(restored.reserve_like(1,30))
        self.assertTrue(restored.reserve_like(cloud.SUI_ROOM,300))
        # Acceptance alone does not release the reserve or permit probe traffic.
        self.assertFalse(restored.reserve_like(1,30))
        restored.record(cloud.SUI_ROOM, {'after':{'like':[10,10]}},cloud.today())
        reloaded = self.state(state.ledger)
        self.assertEqual(reloaded.priority_like_reserve,0)
        self.assertTrue(reloaded.reserve_like(1,300))
        self.assertEqual(reloaded.like_reserved_clicks,5270)

    def test_reservation_without_result_remains_uncertain_after_restart(self):
        state = self.state()
        state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        state.reserve_like(1,30)
        restored = self.state(state.ledger)
        self.assertEqual(restored.like_results,[])
        self.assertEqual(restored.like_reserved_clicks,30)

    def test_run_risk_and_stop_reasons_are_readable_without_lts(self):
        state = self.state()
        row = {'timestamp':123,'risk_code':-352,'like_stops':{'not_live':5}}
        state.record_run_summary(row)
        self.assertEqual(self.state(state.ledger).run_summaries,[row])

    def test_free_actions_saves_each_batch_before_and_after_without_filling_completed_tasks(self):
        state = self.state()
        state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        client = Mock()
        client.gate = cloud.AccountGate()
        client.like_budget = state.reserve_like
        client.like_observer = state.record_like_result
        client.like.side_effect = lambda room,uid,count: client.like_budget(room,count)
        client.room.return_value = {'uid':11,'live_status':1}
        def data(n):
            return {'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':f'每日上限 {n}/2'}]}
        client.tasks.side_effect = [data(1),data(2)]
        with patch('index.time.sleep'):
            result = cloud.free_actions(client,1,11,data(0),cloud.today(),time.monotonic()+60,10)
            cloud.free_actions(client,1,11,result,cloud.today(),time.monotonic()+60,10)
        self.assertEqual(client.like.call_count,2)
        self.assertEqual(state.like_reserved_clicks,60)
        self.assertEqual([(r['before'],r['after']) for r in state.like_results],
                         [([0,2],[1,2]),([1,2],[2,2])])

    def test_offline_target_does_not_consume_like_budget_or_post(self):
        state = self.state()
        state.update_like_plan(adaptive_like_plan(state.header['targets'],{1:1}))
        client = Mock()
        client.gate = cloud.AccountGate()
        client.like_observer = state.record_like_result
        client.room.return_value = {'uid':11,'live_status':0}
        data = {'task_info':[{'jump_type':'like','title':'点赞30次','sub_title':'每日上限 0/10'}]}
        cloud.free_actions(client,1,11,data,cloud.today(),time.monotonic()+60,10)
        client.like.assert_not_called()
        self.assertEqual(state.like_reserved_clicks,0)

    def test_notice_keeps_two_accounts_and_probe_evidence_without_truncation(self):
        identities = [{'account':{'uid':uid,'name':name},'like_target_rooms':list(range(60)),
            'like_level_threshold':12,'like_reserved_clicks':5060,
            'like_priority_reserved_clicks':330,
            'like_probe':{'reserved_clicks':60,'accepted_batches':2,'counter_increased_batches':1},
            'daily_task_completed_rooms':{'like':5,'sendDanmu':80,'watchLive':0}}
            for uid,name in ((123,'主账号'),(456,'副账号'))]
        rows = [{'account':{'uid':uid},'room':cloud.SUI_ROOM,'after':{}} for uid in (123,456)]
        notices = events(cloud.today(),identities,rows,123)
        text = '\n'.join(body for _,body in notices)
        self.assertIn('主账号',text)
        self.assertIn('副账号',text)
        self.assertIn('仅开播执行',text)
        self.assertIn('计数增长1批',text)
        self.assertIn('至少100间',text)
        self.assertIn('优先房间预留330次基础预算',text)
        self.assertIn('此账号仅免费，不送灯牌',text)
        self.assertTrue(all(len(body.encode()) <= 2000 for _,body in notices))
        self.assertEqual(len({key for key,_ in notices}),len(notices))


if __name__ == '__main__':
    unittest.main()
