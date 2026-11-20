"""2026-11-17 新功能:派单回执与重发、一键求助、同事沟通、换表/回绑/归还、电量日志、测试报表。"""
from bycall.core import protocol as P
from bycall.core.watches import WatchError

from tests.test_service import Base, S

NEW = ['receipt', 'help', 'chat', 'bind']


class W(Base):
    def hb(self, w, battery=90, caps=NEW, staff=None, rssi=None, charging=False):
        self.send(P.t_heartbeat(S, w), w, P.HEARTBEAT,
                  dict(battery=battery, caps=caps, staff=staff, wifi_rssi=rssi, charging=charging))

    def up(self, w, type_, payload=None):
        self.send(P.t_up(S, w), w, type_, payload or {})

    def got(self, w, type_):
        return [m for m in self.bus.sent(P.t_dispatch_watch(S, w), type_)]

    def reply(self, w, type_):
        return self.bus.sent(P.t_reply(S, w), type_)

    def tick(self, seconds=0):
        self.clock.advance(seconds)
        self.svc.tick()

    def locate(self, w, beacon):
        self.send(P.t_location(S, w), w, P.LOCATION_REPORT, dict(readings=[dict(beacon_id=beacon, rssi=-50)]))


class TestReceipts(W):
    def test_resend_until_receipt_and_latency(self):
        self.hb('w-01')
        cid = self.call()
        self.assertEqual(len(self.got('w-01', P.CALL_DISPATCH)), 1)
        self.tick(6)                                                    # 5 秒没回执 → 重发
        self.assertEqual(len(self.got('w-01', P.CALL_DISPATCH)), 2)
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_RECEIVED, {}, call_id=cid)
        self.tick(6)
        self.assertEqual(len(self.got('w-01', P.CALL_DISPATCH)), 2)     # 有回执就不再重发
        self.assertEqual(self.svc.latency, [6000])

    def test_no_receipt_marks_unreachable_and_reroutes(self):
        self.hb('w-01'); self.hb('w-02')
        self.call()
        for _ in range(5):
            self.tick(6)
        self.assertIn('n01', self.svc.unreachable)
        self.assertEqual(self.svc.roster.state('n01'), 'offline')
        self.assertTrue(self.got('w-02', P.CALL_DISPATCH))              # 转给同组
        self.assertTrue(any('收不到派单' in p for p in self.svc.watch_problems()))
        self.hb('w-01')                                                 # 还在心跳但不回执:不能恢复为空闲
        self.assertEqual(self.svc.roster.state('n01'), 'offline')

    def test_old_apk_without_caps_is_not_resent(self):
        self.online('w-01')                                             # 老版本心跳不带 caps
        self.call()
        for _ in range(5):
            self.tick(6)
        self.assertEqual(len(self.got('w-01', P.CALL_DISPATCH)), 1)
        self.assertNotIn('n01', self.svc.unreachable)

    def test_battery_log_every_15_minutes(self):
        for i in range(40):                                             # 20 分钟,每 30 秒一次心跳
            self.hb('w-01', battery=90 - i // 10)
            self.clock.advance(30)
        rows = self.svc.delivery.battery_series('2000-01-01')
        self.assertEqual(len(rows), 2)
        r = self.svc.test_report(hours=1)
        self.assertEqual(r['watches'][0]['watch'], 'w-01')


class TestHelp(W):
    def setUp(self):
        super().setUp()
        for w in ('w-01', 'w-02', 'w-03', 'w-04'):
            self.hb(w)
        self.locate('w-01', 'bcn-1601')
        self.locate('w-02', 'bcn-1602')
        self.locate('w-03', 'bcn-c16')

    def help_id(self):
        return self.got('w-01', P.HELP_STATE)[-1]['call_id']

    def test_nearest_free_then_floor_then_leader(self):
        self.up('w-01', P.HELP_REQUEST)
        st = self.got('w-01', P.HELP_STATE)
        self.assertEqual(st[-1]['payload']['status'], 'sent')
        cid = self.help_id()
        notified = [t for t, _ in self.svc.dispatcher.notified(cid)]
        self.assertEqual(sorted(notified), ['n02', 'n03'])               # 同组空闲的 2 人;不含求助人
        d = self.got('w-02', P.CALL_DISPATCH)[-1]['payload']
        self.assertEqual((d['kind'], d['label']), ('help', '演示护理师甲求助'))
        self.assertEqual(self.fb(), [])                                  # 客户平板不能收到同事求助
        self.tick(31)
        self.assertEqual(self.got('w-01', P.HELP_STATE)[-1]['payload']['status'], 'floor')
        self.tick(30)
        self.assertEqual(self.got('w-01', P.HELP_STATE)[-1]['payload']['status'], 'leader')
        self.assertIn('n04', [t for t, _ in self.svc.dispatcher.notified(cid)])
        self.assertIn('station', [t for t, _ in self.svc.dispatcher.notified(cid)])

    def test_accept_tells_requester_and_others(self):
        self.up('w-01', P.HELP_REQUEST)
        cid = self.help_id()
        self.accept('w-01', cid)                                         # 不能接自己的求助
        self.assertEqual(self.reply('w-01', P.CMD_REJECTED)[-1]['payload']['reason'], 'own_help')
        self.accept('w-03', cid)
        self.assertEqual(self.got('w-01', P.HELP_STATE)[-1]['payload']['status'], 'accepted')
        self.assertIn('演示护理师丙', self.got('w-01', P.HELP_STATE)[-1]['payload']['text'])
        self.assertEqual(self.got('w-02', P.CALL_TAKEN)[-1]['call_id'], cid)
        for _ in range(3):                                               # 走到求助人所在房间(定位有防抖,连续几次)→ 自动到场
            self.locate('w-03', 'bcn-1601')
        self.assertEqual(self.got('w-01', P.HELP_STATE)[-1]['payload']['status'], 'arrived')
        self.send(P.t_ack(S, cid), 'w-03', P.CALL_COMPLETE, dict(method='onsite', reason='已协助'), call_id=cid)
        self.assertEqual(self.got('w-01', P.HELP_STATE)[-1]['payload']['status'], 'completed')

    def test_repeat_press_does_not_duplicate_and_requester_can_cancel(self):
        self.up('w-01', P.HELP_REQUEST)
        self.up('w-01', P.HELP_REQUEST)
        self.assertEqual(len(self.svc.core.list_calls(store=S, state='pending')), 1)
        cid = self.help_id()
        self.send(P.t_ack(S, cid), 'w-04', P.CALL_CANCEL, {}, call_id=cid)   # 旁人不能取消
        self.assertEqual(self.reply('w-04', P.CMD_REJECTED)[-1]['payload']['reason'], 'cancel_not_allowed')
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_CANCEL, {}, call_id=cid)
        self.assertEqual(self.got('w-01', P.HELP_STATE)[-1]['payload']['status'], 'cancelled')

    def test_nobody_free_goes_to_floor_at_once(self):
        cid = self.call(room='1602')
        self.accept('w-02', cid)
        cid2 = self.call(room='1601', msg_id='x2')
        self.accept('w-03', cid2)                                        # 同组两人都忙
        self.up('w-01', P.HELP_REQUEST)
        reasons = dict(self.svc.dispatcher.notified(self.help_id()))
        self.assertEqual(reasons.get('n02'), 'help_floor')


class TestChat(W):
    def setUp(self):
        super().setUp()
        for w in ('w-01', 'w-02', 'w-03', 'w-04'):
            self.hb(w)

    def test_floor_group_quick_reply_echo_and_ack(self):
        self.up('w-01', P.CHAT_SEND, dict(kind='quick', text='马上到', client_id='c1'))
        echo = self.got('w-01', P.CHAT_MSG)[-1]['payload']
        self.assertEqual((echo['channel'], echo['client_id'], echo['mine']), ('floor:16', 'c1', True))
        m = self.got('w-02', P.CHAT_MSG)[-1]['payload']
        self.assertEqual((m['sender_name'], m['text'], m['channel_name']), ('演示护理师甲', '马上到', '16楼群'))
        self.up('w-02', P.CHAT_ACK, dict(ids=[m['id']]))
        self.assertEqual(self.svc.chat.undelivered('n02', '2000-01-01'), [])
        self.assertEqual(len(self.svc.chat.undelivered('n03', '2000-01-01')), 1)   # 没回执的,重连会补发

    def test_rate_limit(self):
        self.up('w-01', P.CHAT_SEND, dict(kind='quick', text='收到'))
        self.up('w-01', P.CHAT_SEND, dict(kind='quick', text='收到'))
        self.assertEqual(self.reply('w-01', P.CMD_REJECTED)[-1]['payload']['reason'], 'too_fast')

    def test_queued_while_customer_call_rings(self):
        self.call()                                                      # n01 的手表正在响客户呼叫
        self.up('w-02', P.CHAT_SEND, dict(channel='dm:n01', kind='text', text='你那边忙吗'))
        self.assertEqual(self.got('w-01', P.CHAT_MSG), [])
        self.accept('w-01', self.bus.sent(P.t_feedback(S, '1601'), P.CALL_FEEDBACK)[-1]['call_id'])
        self.tick(1)
        self.assertEqual(self.got('w-01', P.CHAT_MSG)[-1]['payload']['text'], '你那边忙吗')

    def test_broadcast_permission_and_role_call(self):
        self.up('w-01', P.CHAT_SEND, dict(channel='all', kind='text', text='全体注意'))
        self.assertEqual(self.reply('w-01', P.CMD_REJECTED)[-1]['payload']['reason'], 'no_permission')
        self.clock.advance(3)
        self.up('w-04', P.CHAT_SEND, dict(channel='all', kind='text', text='全体注意'))   # 组长可以
        self.assertEqual(self.got('w-03', P.CHAT_MSG)[-1]['payload']['text'], '全体注意')
        self.clock.advance(3)
        self.up('w-01', P.CHAT_SEND, dict(channel='role:cleaner', kind='quick', text='需要帮忙'))
        self.assertEqual(self.reply('w-01', P.CMD_REJECTED)[-1]['payload']['reason'], 'no_one_on_duty')
        self.clock.advance(3)
        self.up('w-01', P.CHAT_SEND, dict(channel='role:leader', kind='quick', text='需要帮忙'))
        m = self.got('w-04', P.CHAT_MSG)[-1]['payload']
        self.assertEqual((m['channel'], m['note']), ('dm:n01|n04', '按岗位呼叫:护士长'))

    def test_contacts_and_history(self):
        self.up('w-01', P.CHAT_SEND, dict(channel='dm:n03', kind='quick', text='收到'))
        c = self.svc.chat_contacts('n01')
        self.assertEqual([x['id'] for x in c['channels']], ['floor:16', 'dept'])
        self.assertEqual(c['people'][0]['staff'], 'n03')                  # 最近联系过的排前面
        h = self.svc.chat_history('n03', 'dm:n01')
        self.assertEqual(h[-1]['text'], '收到')
        with self.assertRaises(ValueError):
            self.svc.chat_history('n02', 'dm:n01|n03')

    def test_voice_message_limits(self):
        self.up('w-01', P.CHAT_SEND, dict(kind='voice', voice_url='/voice/a.m4a', duration_s=45))
        self.assertEqual(self.reply('w-01', P.CMD_REJECTED)[-1]['payload']['reason'], 'voice_too_long')
        self.clock.advance(3)
        self.up('w-01', P.CHAT_SEND, dict(kind='voice', voice_url='/voice/a.m4a', duration_s=12))
        self.assertEqual(self.got('w-02', P.CHAT_MSG)[-1]['payload']['voice_url'], '/voice/a.m4a')


class TestSwap(W):
    def setUp(self):
        super().setUp()
        for w in ('w-01', 'w-02'):
            self.hb(w)
        self.hb('w-09', battery=95)
        self.hb('w-10', battery=40)

    def test_swap_moves_open_calls_to_new_watch(self):
        cid = self.call()
        self.accept('w-01', cid)
        card = self.svc.staff_card('n01')
        self.assertEqual((card['watch']['sn'], card['open_events'], card['rooms'][0]['room']), ('w-01', 1, '1601'))
        r = self.svc.swap('n01', 'w-09')
        self.assertTrue(r['ok'])
        self.assertEqual(self.svc.watch_of('n01'), 'w-09')
        self.assertEqual(self.svc.watches.get('w-01')['status'], 'CHARGING')
        self.assertEqual(self.reply('w-01', P.WATCH_BIND)[-1]['payload']['staff'], None)      # 旧表会话失效
        self.assertEqual(self.reply('w-09', P.WATCH_BIND)[-1]['payload']['name'], '演示护理师甲')
        self.assertEqual(self.got('w-09', P.CALL_DISPATCH)[-1]['call_id'], cid)                # 进行中的单跟着人走
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_ARRIVE, {}, call_id=cid)                     # 旧表的指令不再有效
        self.assertEqual(self.reply('w-01', P.CMD_REJECTED)[-1]['payload']['reason'], 'not_bound')
        self.send(P.t_ack(S, cid), 'w-09', P.CALL_ARRIVE, {}, call_id=cid)
        self.assertEqual(self.fb()[-1], 'arrived')
        acts = [x['action_type'] for x in self.svc.watches.oplog(staff='n01')]
        self.assertEqual(acts, ['SWAP_IN'])
        self.assertEqual(self.svc.swap('n01', 'w-09')['already'], True)                        # 重复提交幂等
        self.assertEqual(len(self.svc.watches.oplog(staff='n01')), 1)

    def test_borrow_checks(self):
        with self.assertRaisesRegex(WatchError, '电量 40%'):
            self.svc.swap('n01', 'w-10')
        with self.assertRaisesRegex(WatchError, '没有编号'):
            self.svc.swap('n01', 'w-99')
        self.svc.swap('n01', 'w-09')
        with self.assertRaisesRegex(WatchError, '已绑定给 演示护理师甲'):
            self.svc.swap('n02', 'w-09')
        self.clock.advance(200)
        self.svc.tick()
        self.hb('w-02')
        with self.assertRaisesRegex(WatchError, '离线'):
            self.svc.swap('n02', 'w-10')
        pool = {p['sn']: p for p in self.svc.spare_pool()}
        self.assertFalse(pool['w-10']['available'])

    def test_rollback_from_watch_button_and_reset(self):
        self.svc.swap('n01', 'w-09')
        self.hb('w-01', battery=50, staff=None)
        self.up('w-09', P.RETURN_HOME)                                   # 原表电量不够:拒绝并说明原因
        self.assertIn('电量 50%', self.reply('w-09', P.CMD_REJECTED)[-1]['payload']['text'])
        self.hb('w-01', battery=96, staff=None)
        self.up('w-01', P.RETURN_HOME)                                   # 在充好电的原表上按
        self.assertEqual(self.svc.watch_of('n01'), 'w-01')
        self.assertEqual(self.svc.watches.get('w-09')['status'], 'RESETTING')
        self.assertTrue(self.reply('w-09', P.WATCH_RESET))
        self.up('w-09', P.RESET_DONE)
        r = self.svc.watches.get('w-09')
        self.assertEqual((r['status'], r['bound_staff']), ('IDLE', None))
        self.assertTrue(r['last_reset_time'])
        acts = [x['action_type'] for x in self.svc.watches.oplog(staff='n01')]
        self.assertEqual(acts[:1], ['ROLLBACK'])
        src = self.svc.watches.oplog(staff='n01')[0]['operate_source']
        self.assertEqual(src, 'WATCH_BUTTON')

    def test_offline_spare_gets_reset_when_it_comes_back(self):
        self.svc.swap('n01', 'w-09')
        self.svc.release('w-09')
        n = len(self.reply('w-09', P.WATCH_RESET))
        self.hb('w-09', battery=90, staff='n01')
        self.assertEqual(len(self.reply('w-09', P.WATCH_RESET)), n + 1)

    def test_heartbeat_with_wrong_binding_gets_corrected(self):
        self.hb('w-02', staff='n01')
        self.assertEqual(self.reply('w-02', P.WATCH_BIND)[-1]['payload']['staff'], 'n02')

    def test_admin_fault_and_board(self):
        self.svc.set_watch_status('w-10', 'FAULT', note='屏幕碎了')
        board = {b['sn']: b for b in self.svc.watch_board()}
        self.assertEqual((board['w-10']['status'], board['w-10']['note']), ('FAULT', '屏幕碎了'))
        self.assertEqual(board['w-01']['bound_name'], '演示护理师甲')
        with self.assertRaises(WatchError):
            self.svc.set_watch_status('w-10', 'BORROWED')

    def test_pool_low_warning(self):
        self.svc.swap('n01', 'w-09')
        self.assertTrue(any('可借出的备用表只剩 0 块' in p for p in self.svc.watch_problems()))


class TestAppUpdatePush(W):
    def test_announce_goes_to_matching_devices(self):
        n = self.svc.announce_app_update('watch', 30000000, '0.3', '设置按钮')
        self.assertEqual(n, 6)                                           # w-01..w-04 + 备用表 w-09、w-10
        m = self.reply('w-09', P.APP_UPDATE)[-1]['payload']
        self.assertEqual((m['app'], m['version_name']), ('watch', '0.3'))
        self.assertEqual(self.reply('tab-1601', P.APP_UPDATE), [])
        self.assertEqual(self.svc.announce_app_update('tablet', 30000000), 2)
        self.assertTrue(self.reply('tab-1602', P.APP_UPDATE))


class TestVoiceMessage(W):
    def test_tablet_voice_reaches_watch_and_station(self):
        """平板语音留言(NEED_NURSE + voice_url):手表派单里带录音地址,大屏数据里也有。"""
        self.hb('w-01')
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST,
                  dict(room='1601', service_code='NEED_NURSE', voice_url='/voice/abc.m4a'))
        d = self.got('w-01', P.CALL_DISPATCH)[-1]['payload']
        self.assertEqual((d['voice_url'], d['label']), ('/voice/abc.m4a', '呼叫护理师'))
        c = self.svc.core.list_calls(store=S, state='pending')[0]
        import json
        self.assertEqual(json.loads(c['request'])['snapshot']['voice_url'], '/voice/abc.m4a')
