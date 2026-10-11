import threading
import time
import unittest
from unittest.mock import Mock, patch

import index as cloud
from test_cloud import Ledger
from test_pacing import Clock


def tasks(watch=0, like=0):
    return {'task_info':[{'jump_type':k,'sub_title':f'每日上限 {n}/2'}
                         for k,n in [('watchLive',watch),('like',like),('sendDanmu',2)]]}


class WatchQueueTests(unittest.TestCase):
    def test_other_room_interactions_continue_during_watch_with_shared_main_thread_checkpoints(self):
        client=Mock(uid=123,watch_seconds=0)
        client.live_statuses.return_value={}
        client.login.return_value={'uid':123}
        client.medal_rooms.return_value={cloud.SUI_ROOM:cloud.SUI_UID,1:11}
        client.tasks.return_value=tasks()
        client.room.return_value={'live_status':1}
        sui_started, other_processed=threading.Event(), threading.Event()
        active, max_active, watched = 0, 0, []
        lock=threading.Lock()
        def watch(account,gate,room,uid,data,day,deadline):
            nonlocal active,max_active
            with lock:
                active+=1
                max_active=max(max_active,active)
                watched.append(room)
            try:
                if room==cloud.SUI_ROOM:
                    sui_started.set()
                    if not other_processed.wait(5):
                        raise AssertionError('Sui watch blocked all other rooms')
                return tasks(watch=2,like=0),60
            finally:
                with lock:
                    active-=1
        def free(client,room,uid,data,day,deadline,max_rounds,audit=None,allow_like=True):
            if room!=cloud.SUI_ROOM:
                if not sui_started.wait(5):
                    raise AssertionError('Sui was not queued first')
                other_processed.set()
            return tasks(like=2)
        ledger=Ledger()
        append=ledger.append_progress
        journal_threads=[]
        def append_on_main(*args):
            journal_threads.append(threading.get_ident())
            append(*args)
        ledger.append_progress=append_on_main
        with patch('index.Bili',return_value=client), patch('index.maybe_gift',return_value='already_done'), \
                patch('index.free_actions',side_effect=free),patch('index.queue_watch',side_effect=watch):
            meta,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},
                                              cloud.today(),time.monotonic()+60,ledger,{})
        self.assertNotIn('status',meta)
        self.assertEqual(watched,[cloud.SUI_ROOM,1])
        self.assertLessEqual(max_active,2)
        self.assertEqual(meta['remaining_rooms'],0)
        self.assertEqual([r['after']['like'] for r in rows],[[2,2],[2,2]])
        self.assertEqual([r['watch_seconds'] for r in rows],[60,60])
        self.assertEqual(set(journal_threads),{threading.get_ident()})

    def test_watch_pool_runs_eight_sessions_concurrently_and_queues_the_rest(self):
        client = Mock(uid=123,watch_seconds=0)
        client.live_statuses.return_value={r:1 for r in range(1,10)}
        client.login.return_value={'uid':123}
        client.medal_rooms.return_value={cloud.SUI_ROOM:cloud.SUI_UID,**{r:100+r for r in range(1,10)}}
        client.tasks.return_value=tasks(watch=0,like=2)
        client.room.return_value={'live_status':1}
        barrier, lock = threading.Barrier(8), threading.Lock()
        active, max_active, starts = 0,0,0
        def watching(account,gate,room,uid,data,day,deadline):
            nonlocal active,max_active,starts
            with lock:
                active+=1
                starts+=1
                current=starts
                max_active=max(max_active,active)
            try:
                if current<=8:
                    barrier.wait(timeout=5)
                return tasks(watch=2,like=2),60
            finally:
                with lock:
                    active-=1
        with patch('index.Bili',return_value=client),patch('index.maybe_gift',return_value='already_done'), \
                patch('index.free_actions',side_effect=lambda *args, **kwargs:args[3]), \
                patch('index.queue_watch',side_effect=watching):
            meta,rows=cloud.run_account_queue({'uid':123,'cookie':'unused','other_medals':True},
                cloud.today(),time.monotonic()+60,Ledger(),{})
        self.assertNotIn('status',meta)
        self.assertEqual(max_active,8)
        self.assertEqual(starts,10)
        self.assertEqual(meta['remaining_rooms'],0)
        self.assertEqual(meta['daily_task_completed_rooms']['watchLive'],10)
        self.assertEqual(sum(row['watch_seconds'] for row in rows),600)

    def test_queued_watch_never_logs_in_after_account_risk(self):
        gate=cloud.AccountGate()
        gate.rejected(-352,cloud.LIKE_ENDPOINT)
        with patch('index.Bili') as bili:
            data=tasks()
            result=cloud.queue_watch({'uid':123,'cookie':'unused'},gate,1,11,data,
                                     cloud.today(),time.monotonic()+60)
        self.assertEqual(result,(data,0))
        bili.assert_not_called()

    def test_sui_has_the_same_bounded_watch_session_as_other_rooms(self):
        for uid in (cloud.SUI_UID,11):
            with self.subTest(uid=uid):
                clock = Clock()
                client = Mock(watch_seconds=0,cookies={'LIVE_BUVID':'example'})
                client.gate = cloud.AccountGate()
                client.room.return_value={'uid':uid,'live_status':1,'parent_area_id':1,'area_id':2}
                data = tasks()
                client.tasks.return_value=data
                state = lambda:{'heartbeat_interval':300,'secret_rule':[0],'timestamp':clock.now}
                with patch('index.time.monotonic',side_effect=lambda:clock.now), \
                        patch('index.time.time',side_effect=lambda:clock.now), \
                        patch('index.time.sleep',side_effect=clock.sleep), \
                        patch('index.enter',side_effect=lambda *args:state()), \
                        patch('index.heartbeat',side_effect=lambda *args:state()) as heartbeat:
                    cloud.watch(client,1,uid,data,cloud.today(),clock.now+6600)
                self.assertEqual(heartbeat.call_count,3)
                self.assertEqual(client.watch_seconds,900)
                self.assertLessEqual(clock.now-1000,960)

    def test_account_risk_interrupts_ongoing_heartbeat_wait(self):
        clock = Clock()
        client = Mock(watch_seconds=0,cookies={'LIVE_BUVID':'example'})
        client.gate = cloud.AccountGate()
        client.room.return_value={'uid':11,'live_status':1,'parent_area_id':1,'area_id':2}
        client.tasks.return_value=tasks()
        clock.on_sleep=lambda:client.gate.rejected(-352,cloud.LIKE_ENDPOINT)
        state={'heartbeat_interval':60,'secret_rule':[0],'timestamp':clock.now}
        with patch('index.time.monotonic',side_effect=lambda:clock.now), \
                patch('index.time.time',side_effect=lambda:clock.now), \
                patch('index.time.sleep',side_effect=clock.sleep), \
                patch('index.enter',return_value=state),patch('index.heartbeat') as heartbeat:
            cloud.watch(client,1,11,tasks(),cloud.today(),clock.now+6600)
        heartbeat.assert_not_called()
        client.tasks.assert_not_called()
        self.assertEqual(client.watch_seconds,0)
        self.assertLess(clock.now,1060)
