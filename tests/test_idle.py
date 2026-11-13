import copy
import time
import unittest
from unittest.mock import patch

import foxtunes as f
import test_web as fixtures
from test_web import A,B


class IdleRecoveryTests(unittest.TestCase):
    setUp=fixtures.WebTests.setUp
    tearDown=fixtures.WebTests.tearDown

    def end_track(self, empty=False):
        self.app.enabled,self.app.device=True,'speaker'
        self.app.now={'uri':A,'duration_ms':100000,'progress_ms':96000,'playing':True}
        self.app.updated=time.time()-5
        self.app.observed_device='speaker'
        self.api.state={'item':None if empty else {'uri':A,'duration_ms':100000},
                        'device':None if empty else {'id':'speaker'},'progress_ms':0,'is_playing':False}
        self.app.poll(True)

    def writes(self):return [c for c in self.api.calls if c[1]!='GET']

    def test_new_request_starts_after_observed_empty_queue_end(self):
        self.end_track()
        self.assertTrue(self.app.enabled)
        self.assertTrue(self.app.health()['idle'])
        self.assertFalse(self.writes())
        self.app.submit(self.sessions[0],B)
        self.app.poll(True)
        self.assertEqual(self.writes(),[('/me/player/play','PUT',{'device_id':'speaker'})])
        self.assertEqual(self.app.queue('')[0]['state'],'accepted')
        self.app.poll(True)
        self.assertEqual(len(self.writes()),1)

    def test_empty_player_response_can_wait_then_start(self):
        self.end_track(empty=True)
        self.assertTrue(self.app.enabled)
        self.app.submit(self.sessions[0],B)
        self.app.poll(True)
        self.assertEqual(len(self.writes()),1)
        self.assertEqual(self.writes()[0][0],'/me/player/play')

    def test_mid_track_or_explicit_pause_does_not_autostart(self):
        self.app.submit(self.sessions[0],B)
        self.app.enabled,self.app.device=True,'speaker'
        self.app.now={'uri':A,'duration_ms':100000,'progress_ms':40000,'playing':True}
        self.app.updated=time.time()-5;self.app.observed_device='speaker'
        self.api.state['is_playing']=False;self.api.state['progress_ms']=40000
        self.app.poll(True)
        self.assertFalse(self.app.idle_ready)
        self.assertFalse(self.writes())
        self.app.sessions[self.sessions[0]]['host']=True
        self.app.action(self.sessions[0],'/api/host/pause',{})
        count=len(self.writes())
        self.app.poll(True)
        self.assertEqual(len(self.writes()),count)
        self.assertFalse(self.app.enabled)

    def test_other_device_never_taken_over(self):
        self.end_track()
        self.app.submit(self.sessions[0],B)
        self.api.state['device']={'id':'other'}
        self.app.poll(True)
        self.assertFalse(self.app.enabled)
        self.assertFalse(self.writes())

    def test_failed_start_held_and_not_retried(self):
        self.end_track()
        self.app.submit(self.sessions[0],B)
        self.api.error=f.Failure('timeout',uncertain=True)
        self.app.poll(True)
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.queue('')[0]['state'],'uncertain')
        self.app.poll(True)
        self.assertEqual(len(self.writes()),1)

    def test_unresolved_handoff_is_not_restarted(self):
        self.end_track()
        self.app.submit(self.sessions[0],B)
        self.app.db.execute("UPDATE songs SET state='accepted'");self.app.db.commit()
        self.app.poll(True)
        self.assertFalse(self.writes())

    def test_stale_or_early_zero_reset_is_not_end(self):
        for elapsed in (1,60):
            self.app.enabled,self.app.device=True,'speaker'
            self.app.now={'uri':A,'duration_ms':100000,'progress_ms':90000,'playing':True}
            self.app.updated=time.time()-elapsed;self.app.observed_device='speaker'
            self.api.state['item']={'uri':A,'duration_ms':100000}
            self.api.state['is_playing']=False;self.api.state['progress_ms']=0
            self.app.poll(True)
            self.assertFalse(self.app.idle_ready)
        self.assertFalse(self.writes())
