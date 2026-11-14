import os, tempfile, time, unittest
from bycall.core.hoststatus import HostStatus
from bycall.core import backup as B
from bycall.core.workflow import Core


class TestHostStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'host.db'); Core(self.db)
        self.bk = os.path.join(self.tmp.name, 'bk')
        self.h = HostStatus(self.tmp.name, self.bk)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_backup_ok(self):
        B.backup(self.db, self.bk)
        s = self.h.snapshot()
        self.assertEqual(s['problems'], [])
        self.assertEqual(s['backup_count'], 1)

    def test_no_backup_grace_then_alert(self):
        self.assertEqual(self.h.snapshot()['problems'], [])                 # 刚启动,宽限
        self.assertIn('还没有数据库备份', self.h.snapshot(time.time() + 700)['problems'])

    def test_stale_and_failed(self):
        B.backup(self.db, self.bk)
        self.assertIn('小时没有新备份', self.h.snapshot(time.time() + 40 * 3600)['problems'][0])
        self.h.backup_error = 'disk full'
        self.assertIn('备份失败', self.h.snapshot()['problems'][0])

    def test_fault_alerts_debounce_and_recover(self):
        B.backup(self.db, self.bk)
        t = time.time()
        self.h.report('mqtt', False, 'MQTT 断开', after_s=15, now=t)
        self.assertEqual(self.h.snapshot()['problems'], [])               # 刚断开,不报(过滤抖动)
        self.h.report('mqtt', False, 'MQTT 断开', after_s=15, now=t + 10)
        self.assertEqual(self.h.snapshot()['problems'], [])
        self.h.report('mqtt', False, 'MQTT 断开', after_s=15, now=t + 16)
        self.assertEqual(self.h.snapshot()['problems'], ['MQTT 断开'])
        self.h.report('sync', False, '同步失败', after_s=0, now=t + 16)
        self.assertEqual(len(self.h.snapshot()['problems']), 2)
        self.h.report('mqtt', True)                                       # 恢复后消除
        self.assertEqual(self.h.snapshot()['problems'], ['同步失败'])
        self.h.report('mqtt', False, 'MQTT 断开', after_s=15, now=t + 100)  # 再次断开重新计时
        self.assertEqual(self.h.snapshot()['problems'], ['同步失败'])

    def test_not_configured(self):
        self.assertIn('未配置', HostStatus(self.tmp.name, None).snapshot()['problems'][0])


if __name__ == '__main__':
    unittest.main()


class TestDeviceOffline(unittest.TestCase):
    def setUp(self):
        from tests.test_service import Base
        self.b = Base(); self.b.setUp()
        self.svc = self.b.svc
        self.h = HostStatus(self.b.tmp.name, None, self.svc, offline_after_s=180)
        self.now = time.time()
        self.h.started = self.now - 3600                 # 主机已运行很久
        self.iso = lambda ago: __import__('datetime').datetime.fromtimestamp(
            self.now - ago, __import__('datetime').timezone.utc).isoformat()

    def tearDown(self):
        self.b.tearDown()

    def beat(self, dev, ago):
        self.svc.tele.heartbeat(dev, self.iso(ago), 90)

    def probs(self):
        return self.h.device_problems(self.now)

    def test_all_online_no_problem(self):
        for d in ('tab-1601', 'tab-1602', 'w-01', 'w-02', 'w-03', 'w-04'):
            self.beat(d, 20)
        self.assertEqual(self.probs(), [])

    def test_single_tablet_and_watch_down(self):
        for d in ('tab-1601', 'w-01', 'w-02', 'w-03', 'w-04'):
            self.beat(d, 20)
        self.beat('tab-1602', 400)                       # 平板 400 秒没心跳
        self.beat('w-02', 400)
        p = self.probs()
        self.assertEqual(len(p), 2)
        self.assertIn('1个房间平板失联超过3分钟:1602', p[0])
        self.assertIn('演示护理师乙', p[1])

    def test_offline_flag_counts_but_short_blip_does_not(self):
        for d in ('tab-1601', 'tab-1602', 'w-01', 'w-02', 'w-03', 'w-04'):
            self.beat(d, 60)
        self.svc.tele.set_online('w-01', False, self.iso(0))   # LWT 刚触发,才失联 60 秒
        self.assertEqual(self.probs(), [])
        self.beat('w-01', 300); self.svc.tele.set_online('w-01', False, self.iso(0))
        self.assertIn('演示护理师甲', self.probs()[0])

    def test_low_battery_watch(self):
        for d in ('tab-1601', 'tab-1602', 'w-01', 'w-02', 'w-03', 'w-04'):
            self.beat(d, 20)
        self.svc.tele.heartbeat('w-02', self.iso(10), 19)
        self.svc.tele.heartbeat('w-03', self.iso(10), 20)         # 恰好 20% 不报
        p = self.probs()
        self.assertEqual(len(p), 1)
        self.assertIn('演示护理师乙(19%)', p[0])
        self.assertNotIn('演示护理师丙', p[0])
        self.svc.tele.heartbeat('w-02', self.iso(8), 19, charging=True)   # 正在充电不再提醒
        self.assertEqual(self.probs(), [])
        self.svc.tele.heartbeat('w-02', self.iso(6), 19, charging=False)
        self.assertEqual(len(self.probs()), 1)
        self.svc.tele.heartbeat('w-02', self.iso(5), 60)          # 电量回升后消除
        self.assertEqual(self.probs(), [])
        # 离线的手表只报离线,不重复报低电量;非在岗的不报
        self.svc.tele.heartbeat('w-04', self.iso(900), 5)
        self.assertEqual([x for x in self.probs() if '电量' in x], [])
        self.svc.tele.heartbeat('w-01', self.iso(5), 5); self.svc.cfg.staff['n01']['on_duty'] = False
        self.assertEqual([x for x in self.probs() if '电量' in x], [])

    def test_everything_down_hints_network(self):
        p = self.probs()                                  # 从未上线且主机已运行 1 小时
        self.assertIn('全部2个房间平板都已失联', p[0])
        self.assertIn('Wi-Fi', p[0])

    def test_host_restart_grace(self):
        self.h.started = self.now - 30                    # 主机刚启动 30 秒,设备还没来得及连
        self.assertEqual(self.probs(), [])

    def test_off_duty_nurse_and_empty_room_ignored(self):
        for d in ('tab-1601', 'tab-1602', 'w-01', 'w-02', 'w-03', 'w-04'):
            self.beat(d, 20)
        self.beat('w-03', 999); self.beat('tab-1602', 999)
        self.svc.cfg.staff['n03']['on_duty'] = False
        self.svc.cfg.rooms['1602']['status'] = 'FREE'
        self.assertEqual(self.probs(), [])
