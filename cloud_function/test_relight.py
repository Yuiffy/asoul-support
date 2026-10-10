import time
import unittest
from unittest.mock import Mock, patch

import index as cloud
from relight_rules import relight_task
from test_cloud import Ledger
from wecom_notify import events


def unlit(storage=False):
    return {'is_lighted':False,'reach_free_intimacy_limit':storage,'task_info':[
        {'jump_type':'sendDanmu','title':'发弹幕10次','sub_title':'仅点亮','is_done':False},
        {'jump_type':'like','title':'点赞30次','sub_title':'仅点亮','is_done':False}]}


def lit():
    return {'is_lighted':True,'task_info':[
        {'jump_type':k,'title':'点赞30次' if k=='like' else k,'sub_title':'每日上限 0/10'}
        for k in ('watchLive','sendDanmu','like')]}


@patch('index.time.sleep')
class RelightTests(unittest.TestCase):
    def client(self,status):
        c=Mock(uid=123,watch_seconds=0)
        c.gate=cloud.AccountGate()
        c.room.return_value={'uid':11,'live_status':status}
        return c

    def test_live_unlit_medal_likes_once_then_exposes_daily_tasks(self,_):
        c=self.client(1)
        c.tasks.return_value=lit()
        result=cloud.free_actions(c,1,11,unlit(),cloud.today(),time.monotonic()+60,1)
        c.like.assert_called_once_with(1,11,30)
        c.danmu.assert_not_called()
        self.assertTrue(result['is_lighted'])
        self.assertTrue(cloud.pending(result,'watchLive'))

    def test_storage_full_does_not_prevent_relighting_but_blocks_daily_likes(self,_):
        c=self.client(1)
        c.tasks.return_value=dict(lit(),reach_free_intimacy_limit=True)
        cloud.free_actions(c,1,11,unlit(True),cloud.today(),time.monotonic()+60,10)
        c.like.assert_called_once_with(1,11,30)

    def test_unconfirmed_like_is_not_blindly_repeated(self,_):
        c=self.client(1)
        c.tasks.return_value=unlit()
        cloud.free_actions(c,1,11,unlit(),cloud.today(),time.monotonic()+60,10)
        c.like.assert_called_once()
        c.danmu.assert_not_called()

    def test_after_relighting_live_room_continues_available_daily_like_rounds(self,_):
        c=self.client(1)
        responses=[lit()]
        for n in range(1,10):
            d=lit(); d['task_info'][2]['sub_title']=f'每日上限 {n}/10'
            responses.append(d)
        c.tasks.side_effect=responses
        result=cloud.free_actions(c,1,11,unlit(),cloud.today(),time.monotonic()+60,10)
        self.assertEqual(c.like.call_count,10)
        self.assertEqual(cloud.summary(result)['like'],[9,10])
        c.danmu.assert_not_called()

    def test_offline_ten_messages_can_relight_without_incremental_api_counter(self,_):
        c=self.client(0)
        c.tasks.side_effect=[unlit() for _ in range(9)]+[lit()]
        cloud.free_actions(c,1,11,unlit(),cloud.today(),time.monotonic()+60,10)
        self.assertEqual(c.danmu.call_count,10)
        c.like.assert_not_called()

    def test_offline_relight_stops_as_soon_as_room_goes_live(self,_):
        c=self.client(0)
        c.room.side_effect=[{'uid':11,'live_status':0},{'uid':11,'live_status':0},{'uid':11,'live_status':1}]
        c.tasks.return_value=unlit()
        cloud.free_actions(c,1,11,unlit(),cloud.today(),time.monotonic()+60,10)
        c.danmu.assert_called_once()

    def test_unknown_or_changed_schema_never_authorizes_actions(self,_):
        for mutate in (lambda d:d.update(is_lighted=None),
                       lambda d:d['task_info'][0].update(sub_title='未知'),
                       lambda d:d['task_info'][1].update(title='点赞300次'),
                       lambda d:d['task_info'][1].update(is_done=0)):
            d=unlit(); mutate(d)
            c=self.client(1)
            cloud.free_actions(c,1,11,d,cloud.today(),time.monotonic()+60,10)
            c.like.assert_not_called(); c.danmu.assert_not_called()

    def test_queue_no_longer_silently_skips_unlit_live_room(self,_):
        c=self.client(1)
        c.login.return_value={'uid':123}
        c.live_statuses.return_value={1:1}
        c.medal_rooms.return_value={1:11}
        empty={'task_info':[],'reach_free_intimacy_limit':True}
        c.tasks.side_effect=lambda uid: empty if uid==cloud.SUI_UID else (lit() if c.like.called else unlit())
        with patch('index.Bili',return_value=c),patch('index.maybe_gift',return_value='already_done'),patch('index.queue_watch',return_value=(lit(),60)):
            meta,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},cloud.today(),time.monotonic()+60,Ledger(),{})
        self.assertNotIn('status',meta)
        row=next(r for r in rows if r['room']==1)
        self.assertFalse(row['before_lighted']); self.assertTrue(row['after_lighted'])
        self.assertTrue(row['relight_only'])
        self.assertEqual(row['watch_seconds'],60)
        message=events(cloud.today(),[meta],rows,123)[0][1]
        self.assertIn('本轮确认点亮1间',message)
        self.assertIn('免费日任务已满0间',message)
