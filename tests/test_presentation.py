"""Presentation checks only: no phone, routing, credentials, or UI window."""
import json
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from presentation import snapshot

class PresentationTests(unittest.TestCase):
    def current(self):
        return dict(state='connected',updated_at=100,stats=dict(active=88,capacity=384,queued=2,dial_waiting=3,failures=1),health=dict(usb=dict(age_seconds=2,latency_ms=30,failures=0),internet=dict(age_seconds=20,failures=0)))
    def test_fresh_health(self):
        d=snapshot(self.current(),'connected',True,102)
        self.assertTrue(d['fresh']);self.assertEqual(d['waiting'],5)
        self.assertEqual(d['usb']['condition'],'good');self.assertEqual(d['internet']['condition'],'good')
    def test_stale_is_unknown(self):
        for now in (99,106):
            d=snapshot(self.current(),'connected',True,now)
            self.assertFalse(d['fresh']);self.assertEqual(d['usb']['condition'],'unknown')
    def test_unreadable_status_does_not_reuse_green(self):
        self.assertEqual(snapshot(self.current(),'connected',False,102)['usb']['condition'],'unknown')
    def test_disconnected_retains_totals_without_live_claim(self):
        d=snapshot(self.current(),'stopped',False,103)
        self.assertEqual(d['failures'],1);self.assertEqual(d['usb']['condition'],'inactive')
    def test_failed_probe_warns_despite_recent_success(self):
        s=self.current();s['health']['internet']['failures']=1
        self.assertEqual(snapshot(s,'connected',True,102)['internet']['condition'],'warning')
    def test_invalid_counters_safe(self):
        s=self.current();s['stats']=dict(active=float('nan'),capacity=float('inf'),failures=-1,queued='oops')
        d=snapshot(s,'connected',True,102)
        self.assertEqual(d['active'],0);self.assertEqual(d['capacity'],0);self.assertEqual(d['failures'],0)
    def test_no_raw_status_or_credentials_exposed(self):
        s=self.current();s.update(token='fixture-secret',serial='fixture-device',message='fixture-sensitive')
        s['stats']['unexpected']='fixture-private'
        text=json.dumps(snapshot(s,'connected',True,102))
        self.assertNotIn('fixture-',text)
    def test_startup_never_claims_health(self):
        self.assertEqual(snapshot()['usb']['condition'],'inactive')

if __name__=='__main__':unittest.main()
