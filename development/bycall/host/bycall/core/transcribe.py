"""留言转文字:本机离线语音识别(faster-whisper),不联网、不用云账号,录音不出店。

模型文件放在主机的 ./models/whisper-small(compose 挂载到 /models/whisper-small),不放进镜像,
这样更新程序时不用重复传 480MB。没有模型时转文字按钮会提示"未启用",其他功能不受影响。
识别结果按录音文件名缓存在 SQLite,同一条留言只识别一次。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing

PROMPT = '以下是月子中心客房里妈妈对护理师的留言,简体中文。'


class TranscribeUnavailable(Exception):
    pass


class Transcriber:
    def __init__(self, db_path, model_dir=None, engine=None):
        self.db_path = str(db_path)
        self.model_dir = model_dir if model_dir is not None else os.environ.get('STT_MODEL_DIR', '/models/whisper-small')
        self._engine = engine            # 测试时可注入:engine(path) -> str
        self._model = None
        self._lock = threading.Lock()    # 一次只识别一条,避免把 N150 的 CPU 占满影响呼叫
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS voice_transcripts(name TEXT PRIMARY KEY, text TEXT NOT NULL, at REAL NOT NULL)')
            db.commit()

    @property
    def available(self):
        return self._engine is not None or os.path.isfile(os.path.join(self.model_dir, 'model.bin'))

    def cached(self, name):
        with closing(sqlite3.connect(self.db_path)) as db:
            r = db.execute('SELECT text FROM voice_transcripts WHERE name=?', (name,)).fetchone()
        return r[0] if r else None

    def cached_many(self, names):
        names = [n for n in set(names) if n]
        if not names:
            return {}
        with closing(sqlite3.connect(self.db_path)) as db:
            q = ','.join('?' * len(names))
            return dict(db.execute(f'SELECT name, text FROM voice_transcripts WHERE name IN ({q})', names).fetchall())

    def _run(self, path):
        if self._engine:
            return self._engine(path)
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise TranscribeUnavailable('主机程序里没有语音识别组件') from e
            self._model = WhisperModel(self.model_dir, device='cpu', compute_type='int8', cpu_threads=4)
        segs, _ = self._model.transcribe(path, language='zh', vad_filter=True, initial_prompt=PROMPT, beam_size=3)
        return ''.join(s.text for s in segs).strip()

    def transcribe(self, name, path):
        """返回 (文字, 是否来自缓存)。"""
        hit = self.cached(name)
        if hit is not None:
            return hit, True
        if not self.available:
            raise TranscribeUnavailable('未安装语音识别模型(把 models 文件夹放到主机 host 目录下)')
        with self._lock:
            hit = self.cached(name)
            if hit is not None:
                return hit, True
            text = self._run(path) or '(没有识别到语音内容)'
            with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
                db.execute('INSERT OR REPLACE INTO voice_transcripts VALUES(?,?,?)', (name, text, time.time()))
                db.commit()
        return text, False
