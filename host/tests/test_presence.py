import os
from datetime import date, timedelta

from fastapi.testclient import TestClient

from bycall.api.app import create_app
from bycall.core.service import HostService
from bycall.core.voice import VoiceStore
from bycall.gateway.bus import InMemoryBus
from tests.test_service import Base


class TestPresence(Base):
    def report(self, start='2026-11-10', end='2026-11-10', **kw):
        return self.svc.presence.daily(date.fromisoformat(start), date.fromisoformat(end), **kw)

    def intervals(self, watch='w-01'):
        return [r for r in self.report()['intervals'] if r['watch'] == watch]

    def test_heartbeat_timeout_reconnect_and_exact_offline_duration(self):
        self.online('w-01')
        self.clock.advance(100); self.svc.tick()
        w = next(r for r in self.svc.presence.snapshot() if r['watch']=='w-01')
        self.assertEqual((w['state'],w['elapsed_seconds']), ('OFFLINE',10))
        self.clock.advance(20); self.online('w-01')
        segments = self.intervals()
        self.assertEqual([(r['state'], r['duration_seconds']) for r in segments], [('ONLINE',90),('OFFLINE',30)])
        self.clock.advance(30)
        self.assertEqual(self.report(watch='w-01')['summary'][0]['online_seconds'], 120)

    def test_repeated_disconnect_does_not_reset_timer_or_duplicate_interval(self):
        self.online('w-01'); self.clock.advance(10)
        self.send('gx/status/w-01','w-01','STATUS',dict(online=False))
        self.clock.advance(20)
        self.send('gx/status/w-01','w-01','STATUS',dict(online=False))
        w = next(r for r in self.svc.presence.snapshot() if r['watch']=='w-01')
        self.assertEqual(w['elapsed_seconds'],20)
        self.assertEqual(len([r for r in self.intervals() if r['state']=='OFFLINE']),1)

    def test_never_connected_is_unknown_not_fabricated_offline(self):
        self.svc.presence.tick(); self.clock.advance(60)
        r = self.report(watch='w-02')['summary'][0]
        self.assertEqual((r['online_seconds'],r['offline_seconds'],r['unknown_seconds']), (0,0,60))

    def test_shanghai_midnight_splits_daily_seconds(self):
        self.clock.advance(13*3600+59*60)  # 北京时间 23:59
        self.online('w-01'); self.clock.advance(60); self.online('w-01'); self.clock.advance(30)
        r=self.report(end='2026-11-11',watch='w-01')['summary']
        self.assertEqual([(x['date'],x['online_seconds']) for x in r], [('2026-11-10',60),('2026-11-11',30)])

    def test_host_restart_gap_is_unknown_and_not_counted_as_attendance(self):
        self.online('w-01'); self.clock.advance(30); self.svc.tick()
        self.clock.advance(300)
        restarted=HostService(self.svc.cfg,InMemoryBus(),self.svc.core.path,self.clock)
        self.clock.advance(20)
        r=restarted.presence.daily(date(2026,11,10),date(2026,11,10),watch='w-01')['summary'][0]
        self.assertEqual((r['online_seconds'],r['offline_seconds'],r['unknown_seconds']), (30,0,320))
        restarted.presence.observe('w-01')
        self.clock.advance(10)
        r=restarted.presence.daily(date(2026,11,10),date(2026,11,10),watch='w-01')['summary'][0]
        self.assertEqual(r['online_seconds'],40)

    def test_binding_changes_split_staff_history_without_resetting_offline_since(self):
        self.online('w-01'); self.clock.advance(10)
        self.send('gx/status/w-01','w-01','STATUS',dict(online=False))
        self.clock.advance(20)
        # 只在虚构快照中模拟换绑，验证区间按有效员工分割。
        self.svc.cfg.staff['n01']['watch']=None
        self.svc.cfg.staff['n02']['watch']='w-01'
        self.svc.presence.tick(); self.clock.advance(10)
        w=next(r for r in self.svc.presence.snapshot() if r['watch']=='w-01')
        self.assertEqual(w['elapsed_seconds'],30)
        a=self.report(watch='w-01',staff='n01')['summary'][0]
        b=self.report(watch='w-01',staff='n02')['summary'][0]
        self.assertEqual((a['offline_seconds'],b['offline_seconds']), (20,10))

    def test_snapshot_has_all_spares_and_at_least_forty_slots(self):
        self.svc.cfg.watches.update({f'test-{i:02}':{} for i in range(45)})
        self.svc.watches.seed()
        c=TestClient(create_app(self.svc,VoiceStore(os.path.join(self.tmp.name,'v')),api_key='k'))
        self.assertEqual(c.get('/station/watch-presence?start=2026-11-10&end=2026-11-10').status_code,401)
        h={'x-api-key':'k'}
        s=c.get('/station/snapshot',headers=h).json()
        self.assertGreaterEqual(s['watch_slots'],49)
        self.assertEqual(len(s['watches']),49)
        self.assertEqual(c.get('/station/watch-presence?start=bad&end=2026-11-10',headers=h).status_code,422)
        self.assertEqual(c.get('/station/watch-presence?start=2026-01-01&end=2026-11-10',headers=h).status_code,422)
        self.assertEqual(c.get('/station/watch-presence?start=2026-11-10&end=2026-11-10&watch=test-00',headers=h).status_code,200)

    def test_location_samples_include_binding_and_stationary_minute(self):
        self.online('w-01')
        readings=dict(readings=[dict(beacon_id='bcn-1601',rssi=-55)])
        self.send('gx/location/w-01','w-01','LOCATION_REPORT',readings)
        self.clock.advance(30);self.send('gx/location/w-01','w-01','LOCATION_REPORT',readings)
        self.clock.advance(30);self.send('gx/location/w-01','w-01','LOCATION_REPORT',readings)
        c=TestClient(create_app(self.svc,VoiceStore(os.path.join(self.tmp.name,'v')),api_key='k'))
        url='/station/watch-activity?start=2026-11-10&end=2026-11-10&staff=n01&limit=1'
        page=c.get(url,headers={'x-api-key':'k'}).json()
        self.assertEqual(len(page['items']),1);self.assertTrue(page['has_more'])
        self.assertEqual(page['items'][0]['store'],'gx');self.assertTrue(page['items'][0]['binding'])
        second=c.get(url+'&after='+str(page['next_after']),headers={'x-api-key':'k'}).json()
        self.assertFalse(second['has_more']);self.assertEqual(len(second['items']),1)
