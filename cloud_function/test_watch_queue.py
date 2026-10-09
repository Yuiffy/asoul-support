import threading
import time
import unittest
from unittest.mock import Mock, patch

import index as cloud
from test_cloud import Ledger


def tasks(watch=0, like=0):
    return {'task_info':[{'jump_type':k,'sub_title':f'每日上限 {n}/2'}
                         for k,n in [('watchLive',watch),('like',like),('sendDanmu',2)]]}


class WatchQueueTests(unittest.TestCase):
    def test_other_room_interactions_continue_during_sui_watch_with_one_watcher(self):
        client=Mock(uid=123,watch_seconds=0)
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
        def free(client,room,uid,data,day,deadline,max_rounds):
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
        self.assertEqual(max_active,1)
        self.assertEqual(meta['remaining_rooms'],0)
        self.assertEqual([r['after']['like'] for r in rows],[[2,2],[2,2]])
        self.assertEqual([r['watch_seconds'] for r in rows],[60,60])
        self.assertEqual(set(journal_threads),{threading.get_ident()})

    def test_queued_watch_never_logs_in_after_account_risk(self):
        gate=cloud.AccountGate()
        gate.rejected(-352,cloud.LIKE_ENDPOINT)
        with patch('index.Bili') as bili:
            data=tasks()
            result=cloud.queue_watch({'uid':123,'cookie':'unused'},gate,1,11,data,
                                     cloud.today(),time.monotonic()+60)
        self.assertEqual(result,(data,0))
        bili.assert_not_called()
