import os, tempfile, hashlib, unittest
from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core.appupdate import AppUpdates, UpdateError
from bycall.core.voice import VoiceStore
from tests.test_service import Base

APK = b'PK\x03\x04' + b'x' * 5000


class TestAppUpdate(Base):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.upd = AppUpdates(os.path.join(d, 'apk'))
        self.c = TestClient(create_app(self.svc, VoiceStore(os.path.join(d, 'v')), api_key='k', app_updates=self.upd))
        self.h = {'x-api-key': 'k'}

    def pub(self, code, app='watch', body=APK, **kw):
        return self.c.post(f'/app/publish/{app}', params=dict(version_code=code, version_name='0.2', notes='修复', **kw),
                           content=body, headers=self.h)

    def test_flow(self):
        self.assertEqual(self.c.get('/app/update?app=watch').status_code, 401)
        self.assertFalse(self.c.get('/app/update?app=watch', headers=self.h).json()['available'])
        self.assertEqual(self.c.get('/app/download/watch', headers=self.h).status_code, 404)
        r = self.pub(100)
        self.assertEqual(r.status_code, 200)
        m = self.c.get('/app/update?app=watch', headers=self.h).json()
        self.assertEqual((m['available'], m['versionCode'], m['versionName'], m['notes']), (True, 100, '0.2', '修复'))
        self.assertEqual(m['sha256'], hashlib.sha256(APK).hexdigest())
        body = self.c.get('/app/download/watch', headers=self.h).content
        self.assertEqual(body, APK)
        self.assertFalse(self.c.get('/app/update?app=tablet', headers=self.h).json()['available'])   # per app

    def test_rejects(self):
        self.pub(100)
        self.assertEqual(self.pub(100).status_code, 422)                       # not newer
        self.assertEqual(self.pub(99).status_code, 422)                        # downgrade
        self.assertEqual(self.pub(101, body=b'not an apk').status_code, 422)
        self.assertEqual(self.pub(101, body=b'').status_code, 422)
        self.assertEqual(self.pub(0).status_code, 422)
        self.assertEqual(self.pub(101, app='phone').status_code, 422)
        self.assertEqual(self.c.get('/app/update?app=../x', headers=self.h).status_code, 422)
        self.assertEqual(self.pub(100, force='true').status_code, 200)         # explicit override
        self.assertEqual(self.pub(101).status_code, 200)
        self.assertEqual(self.upd.info('watch')['versionCode'], 101)

    def test_persists(self):
        self.pub(7)
        self.assertEqual(AppUpdates(self.upd.dir).info('watch')['versionCode'], 7)


if __name__ == '__main__':
    unittest.main()
