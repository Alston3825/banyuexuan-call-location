import os, tempfile, unittest
from datetime import datetime, timezone
from bycall.core import protocol as P
from bycall.core.config import StoreConfig
from bycall.core.service import HostService
from bycall.gateway.bus import InMemoryBus


class TestLoad(unittest.TestCase):
    def test_30_rooms_all_answered_exactly_once(self):
        rooms = {str(2300 + i): dict(floor='23', tablet=f'tab-{2300+i}') for i in range(1, 31)}
        staff = {f'n{i:02d}': dict(name=f'护理师{i}', watch=f'w-{i:02d}') for i in range(1, 11)}
        cfg = StoreConfig(store_id='gx', rooms=rooms, beacons={}, staff=staff,
                          groups={'g': list(staff)}, assignments={r: {'day': 'n01', 'night': 'n01'} for r in rooms},
                          leaders={'g': 'n01'}, request_types={'MILK_STORAGE': '取储存母乳'})
        with tempfile.TemporaryDirectory() as d:
            bus = InMemoryBus()
            svc = HostService(cfg, bus, os.path.join(d, 'h.db'), lambda: datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc))
            for s in staff.values():
                bus.publish(P.t_heartbeat('gx', s['watch']), P.Envelope(store_id='gx', device_id=s['watch'], type=P.HEARTBEAT, payload={}).model_dump())

            def bot(topic, msg):          # every watch accepts every dispatch it receives
                if msg['type'] == P.CALL_DISPATCH:
                    w = topic.split('/')[-1]
                    bus.publish(P.t_ack('gx', msg['call_id']), P.Envelope(store_id='gx', device_id=w, type=P.CALL_ACCEPT, call_id=msg['call_id']).model_dump())
            bus.subscribe('gx/dispatch/+', bot)
            for r, v in rooms.items():
                bus.publish(P.t_call('gx', '23', r), P.Envelope(store_id='gx', device_id=v['tablet'], type=P.CALL_REQUEST,
                                                                 payload=dict(room=r, service_code='MILK_STORAGE')).model_dump())
            for r in rooms:
                st = [m['payload']['status'] for m in bus.sent(P.t_feedback('gx', r))]
                self.assertEqual(st.count('received'), 1, r)
                self.assertEqual(st.count('accepted'), 1, r)
            self.assertEqual(len(svc.core.list_calls(store='gx', limit=500)), 30)


if __name__ == '__main__':
    unittest.main()
