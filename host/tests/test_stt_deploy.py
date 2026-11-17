import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bycall.core.stt_model import ModelError, resolve_model
from bycall.core.transcribe import Transcriber, TranscribeUnavailable


def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    def model(self, name='whisper-small'):
        p=self.root/name;p.mkdir()
        (p/'model.bin').write_bytes(b'weights-test')
        (p/'config.json').write_text('{}');(p/'tokenizer.json').write_text('{}')
        return p

    def test_model_name_and_direct_directory(self):
        m=self.model('whisper-samill')
        self.assertEqual(resolve_model(self.root),str(m))
        self.assertEqual(resolve_model(m),str(m))

    def test_missing_tokenizer_lfs_and_native_pt_rejected(self):
        m=self.model();(m/'tokenizer.json').unlink()
        with self.assertRaises(ModelError):resolve_model(self.root)
        (m/'tokenizer.json').write_text('{}')
        (m/'model.bin').write_text('version https://git-lfs.github.com/spec/v1')
        with self.assertRaises(ModelError):resolve_model(self.root)
        (m/'model.bin').unlink();(m/'small.pt').write_bytes(b'pt')
        with self.assertRaises(ModelError):resolve_model(self.root)

    def test_multiple_models_require_selection(self):
        self.model('one');two=self.model('two')
        with self.assertRaises(ModelError):resolve_model(self.root)
        self.assertEqual(resolve_model(two),str(two))

    def test_model_load_uses_offline_cpu_and_int8(self):
        self.model();calls=[]
        fake=types.ModuleType('faster_whisper')
        fake.WhisperModel=lambda path,**kw:calls.append((path,kw)) or object()
        with patch.dict(sys.modules,{'faster_whisper':fake}),patch.dict(os.environ,STT_CPU_THREADS='2'):
            t=Transcriber(self.root/'cache.db',model_dir=str(self.root))
            self.assertTrue(t.available);self.assertFalse(t.status()['loaded'])
            self.assertTrue(t.load_model()['loaded'])
            self.assertEqual(calls[0][1],dict(device='cpu',compute_type='int8',cpu_threads=2,local_files_only=True))

    def test_invalid_weights_returns_clear_unavailable(self):
        self.model();fake=types.ModuleType('faster_whisper')
        def fail(*a,**kw):raise ValueError('bad weights')
        fake.WhisperModel=fail
        with patch.dict(sys.modules,{'faster_whisper':fake}):
            t=Transcriber(self.root/'cache.db',model_dir=str(self.root))
            with self.assertRaises(TranscribeUnavailable):t.load_model()
            self.assertIn('bad weights',t.status()['error'])

    def test_concurrent_same_voice_only_computes_once(self):
        calls=[]
        def engine(path):calls.append(path);time.sleep(.03);return '护理师请过来'
        t=Transcriber(self.root/'cache.db',engine=engine)
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:t.transcribe('same','voice'),range(2)))
        self.assertEqual(len(calls),1)
        self.assertEqual(sorted(r[1] for r in results),[False,True])


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        base=Path(__file__).resolve().parents[1]
        self.local=module('deploy_local',base/'deploy/deploy_n100.py')
        self.remote=module('deploy_remote',base/'deploy/n100_remote.py')
        (self.root/'host/bycall').mkdir(parents=True)
        (self.root/'host/tools').mkdir()
        (self.root/'host/bycall/main.py').write_text('x=1')
        (self.root/'host/requirements.txt').write_text('test-dependency\n')
        (self.root/'host/bycall/hidden.db').write_text('private')
        (self.root/'host/models').mkdir();(self.root/'host/models/model.bin').write_text('private weights')
        (self.root/'host/.env').write_text('secret')
        self.archive,self.meta,self.digest=self.local.package(self.root,self.root/'out')

    def tearDown(self):self.tmp.cleanup()

    def test_package_omits_runtime_data_and_stamps_version(self):
        with tarfile.open(self.archive) as t:
            names=t.getnames()
            self.assertFalse(any('.env' in n or '.db' in n or 'models' in n for n in names))
            self.assertEqual(t.extractfile('bycall/VERSION').read().decode().strip(),self.meta['version'])
        out=self.root/'extract';out.mkdir()
        self.assertEqual(self.remote.extract_payload(self.archive,out,self.digest)['version'],self.meta['version'])

    def test_changed_archive_and_traversal_rejected(self):
        out=self.root/'extract';out.mkdir()
        with self.assertRaises(self.remote.DeployError):self.remote.extract_payload(self.archive,out,'0'*64)
        unsafe=self.root/'unsafe.tar.gz'
        with tarfile.open(unsafe,'w:gz') as t:
            m=tarfile.TarInfo('../secret');m.size=1;t.addfile(m,io.BytesIO(b'x'))
        with self.assertRaises(self.remote.DeployError):
            self.remote.extract_payload(unsafe,out,hashlib.sha256(unsafe.read_bytes()).hexdigest())
        self.assertFalse((self.root/'secret').exists())

    def test_first_config_never_overwrites_and_hides_secrets(self):
        project=self.root/'project';(project/'config').mkdir(parents=True)
        (project/'mosquitto/auth').mkdir(parents=True)
        (project/'config/store.json').write_text('{}')
        (project/'mosquitto/auth/credentials.json').write_text(json.dumps({'host':'temporary-test'}))
        self.remote.initialize_env(project,'192.168.1.108')
        before=(project/'.env').read_bytes()
        self.assertEqual(stat.S_IMODE((project/'.env').stat().st_mode),0o600)
        with self.assertRaises(self.remote.DeployError):self.remote.initialize_env(project,'192.168.1.108')
        self.assertEqual((project/'.env').read_bytes(),before)

    def test_health_failure_restores_image_not_database(self):
        project=self.root/'project';project.mkdir()
        (project/'.env').write_text('unchanged-env')
        (project/'docker-compose.yml').write_text('services: {}')
        data=project/'data';data.mkdir();db=sqlite3.connect(data/'host.db');db.execute('CREATE TABLE x(v)');db.execute('INSERT INTO x VALUES(7)');db.commit();db.close()
        old=dict(Image='sha256:old',State=dict(Running=True),Mounts=[dict(Source=str(data),Destination='/data',Type='bind')])
        config=dict(services=dict(host=dict(image='bycall-host:test',environment={})))
        ops=[]
        def compose(p,*args,**kw):
            ops.append(args)
            if args[:2]==('config','--format'):return json.dumps(config)
            if args[:2]==('ps','-q'):return 'old-container'
            return ''
        tags=[]
        def run(args,**kw):
            if args[:2]==['docker','tag']:tags.append(args[2:]);return ''
            if 'requirements.txt' in str(args):return hashlib.sha256(b'test-dependency\n').hexdigest()
            if 'bycall.version' in str(args):return 'old-version'
            return ''
        with patch.object(self.remote,'compose',side_effect=compose),patch.object(self.remote,'run',side_effect=run),patch.object(self.remote,'inspect',return_value=old),patch.object(self.remote,'wait_health',side_effect=[False,True]),patch.object(self.remote.platform,'machine',return_value='x86_64'),patch.object(self.remote.os,'geteuid',return_value=0):
            with self.assertRaises(self.remote.DeployError):self.remote.apply(project,self.archive,self.digest)
        self.assertIn(['sha256:old','bycall-host:test'],tags)
        self.assertIn(('stop','-t','20','host'),ops)
        self.assertEqual((project/'.env').read_text(),'unchanged-env')
        db=sqlite3.connect(data/'host.db');self.assertEqual(db.execute('SELECT v FROM x').fetchone()[0],7);db.close()
        self.assertTrue((project/'upgrade-backups'/self.meta['version']/'host.db').exists())
