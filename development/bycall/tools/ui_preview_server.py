"""主机界面预览用的模拟主机：只返回明确标记的虚构数据，不连接真实主机。
用法: python3 -I ui_preview_server.py <host/bycall 目录> <端口> [full|few|warn]
然后浏览器打开 http://127.0.0.1:<端口>/station?key=demo"""
import json, sys, random, datetime as dt
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
HTML = sys.argv[1]; PORT = int(sys.argv[2]); MODE = sys.argv[3] if len(sys.argv) > 3 else 'full'
def iso(sec_ago): return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=sec_ago)).isoformat()
AREAS = ['2401 房间', '2402 房间', '托管室北侧', 'SPA 馆南侧', '护士站', '走廊东段', '2405 房间']
def watches():
    out = []
    n = 60 if MODE != 'few' else 6
    for i in range(1, n + 1):
        off = i % 9 == 0
        w = dict(sn=f'w-{i:02d}', bound_name=f'演示护理师{i:02d}', bound_staff=f's{i}', online=not off,
                 last_seen=iso(890 if off else 5), offline_since=iso(890) if off else None,
                 battery=30 + (i * 7) % 70, charging=(i % 13 == 0), status='OK',
                 heart_rate=None if off else 64 + i % 30, heart_rate_state='stale' if off else 'fresh', heart_rate_at=iso(20),
                 blood_pressure=None if i % 2 == 0 or off else {'systolic': 110 + i % 20, 'diastolic': 70 + i % 12},
                 blood_pressure_state='fresh' if (i % 2 and not off) else 'no_data', blood_pressure_at=iso(30),
                 location_status='stale' if off else ('unknown' if i % 7 == 0 else 'fresh'),
                 location_area=AREAS[i % len(AREAS)], located_at=iso(3), location_ttl_seconds=30,
                 location_valid_until=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=25)).isoformat())
        if i == 5: w['last_seen'] = iso(70)
        out.append(w)
    if MODE == 'few':
        out.append(dict(sn='w-07', spare=True, online=False, last_seen=None, battery=None, location_status='unknown', location_reason='unbound'))
    return out
def snapshot():
    active = [] if MODE == 'few' else [
        dict(call_id='c1', room='1601', floor=16, state='pending', priority='emergency', label='紧急呼叫', created_at=iso(42)),
        dict(call_id='c2', room='1605', floor=16, state='pending', label='需要换尿布', created_at=iso(18), text=''),
        dict(call_id='c3', room='2402', floor=24, state='claimed', label='喂奶协助', owner_name='演示护理师03', created_at=iso(95), voice_url='/voice/x.ogg', transcript='宝宝一直在哭，麻烦过来看一下'),
        dict(call_id='c4', room='2405', floor=24, state='arrived', label='呼叫护理师', owner_name='演示护理师11', created_at=iso(260)),
        dict(call_id='c5', room='1610', floor=16, kind='help', state='claimed', label='同事求助', owner_name='演示护理师22', created_at=iso(130)),
    ]
    recent = [dict(call_id=f'r{i}', room=r, label=l, state='completed', owner_name=f'演示护理师{i+4:02d}', created_at=iso(900 + i * 300), ended_at=iso(700 + i * 300))
              for i, (r, l) in enumerate([('1602', '呼叫护理师'), ('2401', '送水'), ('1608', '喂奶协助'), ('2403', '需要换尿布'), ('1603', '呼叫护理师'), ('2406', '紧急呼叫')])]
    rooms = [dict(room=r, online=r not in ('1601', '1602'), battery=80, guest_buttons=[]) for r in ['1601', '1602', '1603', '1605', '2401', '2402', '2405']]
    return dict(store_id='gx', version='20261121-ui-dev', now=iso(0), active=active, recent=recent, watches=watches(), rooms=rooms,
                host=dict(problems=['演示：备份超过 24 小时未完成'] if MODE == 'warn' else [], backup_at=dt.datetime.now().timestamp() - 3600, disk_free_gb=182),
                location_state=dict(consumer_timeout_seconds=8))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, body, ctype='application/json'):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = urlparse(self.path).path
        pages={'/station':'api/station.html','/admin/terminals':'api/terminals.html','/swap':'api/swap.html','/report':'api/report.html','/backup':'api/backup.html','/update':'updater/update.html'}
        if p in pages: return self.send(200, open(HTML+'/'+pages[p], 'rb').read(), 'text/html; charset=utf-8')
        if p == '/station/snapshot': return self.send(200, snapshot())
        if p == '/system/update': return self.send(200, {'updater': True, 'pending': None, 'job': {}})
        if p.startswith('/terminal-config/station/'): return self.send(200, {'revision': 0, 'settings': {}})
        return self.send(404, {'detail': 'nf'})
    def do_POST(self): return self.send(200, {})
ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
