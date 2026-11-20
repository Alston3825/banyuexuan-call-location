"""Check a running broker enforces the generated accounts/ACL.
  python tools/mqtt_acl_check.py <broker-host> <credentials.json> [store]
Publishes with each device account and watches (as `host`) what actually gets through."""
import json, sys, time
import paho.mqtt.client as mqtt

HOST, CRED = sys.argv[1], json.load(open(sys.argv[2]))
S = sys.argv[3] if len(sys.argv) > 3 else 'gx'
fails = []


def client(user, pw):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='chk-' + user)
    if pw is not None:
        c.username_pw_set(user, pw)
    return c


def check(name, ok):
    print(('PASS ' if ok else 'FAIL ') + name)
    if not ok:
        fails.append(name)


# 1. anonymous and wrong password are refused
for label, u, p in (('anonymous', None, None), ('wrong password', 'w-01', 'nope')):
    c = client(u or 'anon', p); rc = []
    c.on_connect = lambda cl, ud, fl, reason, pr=None: rc.append(reason)
    c.connect(HOST); c.loop_start(); time.sleep(1); c.loop_stop()
    check(f'{label} refused', bool(rc) and rc[0].is_failure)

# 2. what gets through a device's publishes
seen = []
h = client('host', CRED['host'])
h.on_message = lambda cl, ud, m: seen.append(m.topic)
h.connect(HOST); h.subscribe(f'{S}/#'); h.loop_start(); time.sleep(0.5)


def pub(user, topic):
    c = client(user, CRED[user]); c.connect(HOST); c.loop_start(); time.sleep(0.3)
    c.publish(topic, '{}', qos=1).wait_for_publish(); time.sleep(0.3); c.loop_stop(); c.disconnect()


cases = [('w-01', f'{S}/heartbeat/w-01', True), ('w-01', f'{S}/heartbeat/w-02', False),
         ('w-01', f'{S}/location/w-02', False), ('w-01', f'{S}/call/16/1601', False),
         ('w-01', f'{S}/dispatch/w-02', False), ('w-01', f'{S}/feedback/1601', False),
         ('tab-1601', f'{S}/call/16/1601', True), ('tab-1601', f'{S}/call/16/1602', False),
         ('tab-1601', f'{S}/vitals/w-01', False), ('tab-1601', f'{S}/dispatch/w-01', False)]
for user, topic, allowed in cases:
    seen.clear(); pub(user, topic)
    check(f'{user} publish {topic} {"allowed" if allowed else "blocked"}', (topic in seen) == allowed)

# 3. subscriptions: judged by whether messages actually reach the subscriber (MQTT 3 brokers may
#    still return a granted SUBACK for an ACL-denied filter but never deliver)
for user, filt, topic, allowed in [
        ('w-01', f'{S}/dispatch/w-01', f'{S}/dispatch/w-01', True),
        ('w-01', f'{S}/dispatch/w-02', f'{S}/dispatch/w-02', False),
        ('w-01', f'{S}/#', f'{S}/call/16/1601', False),
        ('tab-1601', f'{S}/feedback/1601', f'{S}/feedback/1601', True),
        ('tab-1601', f'{S}/feedback/1602', f'{S}/feedback/1602', False)]:
    got = []
    c = client(user, CRED[user]); c.on_message = lambda cl, ud, m: got.append(m.topic)
    c.connect(HOST); c.loop_start(); time.sleep(0.3); c.subscribe(filt, qos=1); time.sleep(0.5)
    h.publish(topic, '{}', qos=1).wait_for_publish(); time.sleep(0.5); c.loop_stop()
    check(f'{user} subscribe {filt} {"receives" if allowed else "receives nothing"}', bool(got) == allowed)
print('ALL OK' if not fails else f'{len(fails)} FAILED'); sys.exit(1 if fails else 0)
