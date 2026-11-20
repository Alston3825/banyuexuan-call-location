"""Off-site backup: client-side encryption + S3-compatible object storage (Aliyun OSS, Tencent COS,
Huawei OBS, Qiniu, MinIO, AWS S3 ...). Data is encrypted on the host BEFORE upload; the cloud only
ever sees ciphertext. The passphrase lives in the host's .env and must also be kept somewhere safe by
the owner: without it a cloud backup can NOT be decrypted."""
from __future__ import annotations

import os
import shutil
from typing import List

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b'BYCB1'
SUFFIX = '.enc'


class CloudError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def encrypt_file(src: str, dst: str, passphrase: str):
    if not passphrase:
        raise CloudError('未设置云备份加密口令,为保护数据不会上传')
    salt, nonce = os.urandom(16), os.urandom(12)
    with open(src, 'rb') as f:
        data = f.read()
    ct = AESGCM(_key(passphrase, salt)).encrypt(nonce, data, MAGIC)
    with open(dst, 'wb') as f:
        f.write(MAGIC + salt + nonce + ct)


def decrypt_file(src: str, dst: str, passphrase: str):
    with open(src, 'rb') as f:
        blob = f.read()
    if blob[:5] != MAGIC or len(blob) < 5 + 16 + 12 + 16:
        raise CloudError('不是本系统的加密备份文件')
    salt, nonce, ct = blob[5:21], blob[21:33], blob[33:]
    try:
        data = AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, MAGIC)
    except InvalidTag:
        raise CloudError('解密失败:口令不对,或备份文件已损坏')
    with open(dst, 'wb') as f:
        f.write(data)


class DirStore:
    """Folder as 'cloud' (tests, or a NAS / mounted network drive)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def put(self, local: str, name: str):
        tmp = os.path.join(self.path, name + '.part')
        shutil.copyfile(local, tmp)
        os.replace(tmp, os.path.join(self.path, name))

    def get(self, name: str, local: str):
        try:
            shutil.copyfile(os.path.join(self.path, name), local)
        except OSError as e:
            raise CloudError(str(e))

    def list(self) -> List[dict]:
        out = []
        for n in sorted(os.listdir(self.path)):
            if n.endswith(SUFFIX):
                st = os.stat(os.path.join(self.path, n))
                out.append(dict(name=n, size=st.st_size, mtime=st.st_mtime))
        return out

    def delete(self, name: str):
        os.remove(os.path.join(self.path, name))


class S3Store:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 prefix: str = 'bycall/', region: str = None):
        import boto3
        from botocore.config import Config
        self.bucket = bucket
        self.prefix = prefix if (not prefix or prefix.endswith('/')) else prefix + '/'
        virtual = bool(endpoint) and any(h in endpoint for h in ('aliyuncs.com', 'myqcloud.com'))
        kw = dict(connect_timeout=10, read_timeout=60, retries={'max_attempts': 3},
                  s3={'addressing_style': 'virtual'} if virtual else {})
        try:                                   # 新版 SDK 默认附加校验和头,部分国内 S3 兼容存储不认
            cfg = Config(request_checksum_calculation='when_required',
                         response_checksum_validation='when_required', **kw)
        except TypeError:
            cfg = Config(**kw)
        self.c = boto3.client('s3', endpoint_url=endpoint or None, region_name=region or None,
                              aws_access_key_id=access_key, aws_secret_access_key=secret_key, config=cfg)

    def _wrap(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:                       # botocore errors are many; surface a short text
            raise CloudError(f'{type(e).__name__}: {str(e)[:150]}')

    def put(self, local: str, name: str):
        self._wrap(self.c.upload_file, local, self.bucket, self.prefix + name)

    def get(self, name: str, local: str):
        self._wrap(self.c.download_file, self.bucket, self.prefix + name, local)

    def list(self) -> List[dict]:
        out, token = [], None
        while True:
            kw = dict(Bucket=self.bucket, Prefix=self.prefix)
            if token:
                kw['ContinuationToken'] = token
            r = self._wrap(self.c.list_objects_v2, **kw)
            for o in r.get('Contents', []):
                n = o['Key'][len(self.prefix):]
                if n.endswith(SUFFIX) and '/' not in n:
                    out.append(dict(name=n, size=o['Size'], mtime=o['LastModified'].timestamp()))
            if not r.get('IsTruncated'):
                return sorted(out, key=lambda x: x['name'])
            token = r.get('NextContinuationToken')

    def delete(self, name: str):
        self._wrap(self.c.delete_object, Bucket=self.bucket, Key=self.prefix + name)


def store_from_env(env=os.environ):
    """None when cloud backup is not configured."""
    if env.get('CLOUD_DIR'):
        return DirStore(env['CLOUD_DIR'])
    if env.get('CLOUD_BUCKET') and env.get('CLOUD_ACCESS_KEY') and env.get('CLOUD_SECRET_KEY'):
        return S3Store(env.get('CLOUD_ENDPOINT', ''), env['CLOUD_BUCKET'], env['CLOUD_ACCESS_KEY'],
                       env['CLOUD_SECRET_KEY'], env.get('CLOUD_PREFIX', 'bycall/'), env.get('CLOUD_REGION'))
    return None
