#!/usr/bin/env python3
"""N100 升级执行器：由桌面端暂存后，以交互 sudo 调用；不保存密码。"""
import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


class DeployError(RuntimeError):
    pass


def run(args, timeout=180, capture=True):
    r = subprocess.run([str(x) for x in args], capture_output=capture, text=True, timeout=timeout)
    if r.returncode:
        raise DeployError('命令失败：' + str(args[0]) + ('；' + (r.stderr or '')[-500:] if capture else ''))
    return r.stdout.strip() if capture else ''


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''):
            h.update(part)
    return h.hexdigest()


def compose(project, *args, capture=True):
    return run(['docker', 'compose', '--project-directory', project, *args], timeout=300, capture=capture)


def inspect(cid):
    return json.loads(run(['docker', 'inspect', cid]))[0]


def check(project):
    models = []
    root = project / 'models'
    if root.is_dir():
        paths = ([root / 'model.bin'] if (root/'model.bin').is_file() else list(root.glob('*/model.bin')))
        for f in paths:
            models.append(dict(path=str(f.parent), bytes=f.stat().st_size,
                               files_complete=all((f.parent/n).is_file() for n in ('config.json','tokenizer.json'))))
    r = subprocess.run(['docker','ps','--format','{{.Names}} {{.Status}}'], capture_output=True, text=True)
    print(json.dumps(dict(host=platform.node(), architecture=platform.machine(), project=str(project),
                          env_exists=(project/'.env').is_file(), models=models,
                          docker_access=r.returncode == 0, services=r.stdout.splitlines() if r.returncode==0 else [],
                          note='只读检查；未读取密钥或业务记录，未重启服务'), ensure_ascii=False, indent=2))


def extract_payload(archive, dest, expected):
    if sha(archive) != expected:
        raise DeployError('传输包 SHA256 不一致')
    total = 0
    with tarfile.open(archive, 'r:gz') as tar:
        for m in tar:
            name = Path(m.name)
            if name.is_absolute() or '..' in name.parts or not name.parts or not m.isfile():
                raise DeployError('升级包含不允许的条目')
            if name.parts[0] not in ('bycall','tools','requirements.txt','release.json'):
                raise DeployError('升级包包含配置或运行数据，已拒绝')
            if name.parts[0] in ('bycall','tools') and not (name.suffix in ('.py','.html') or name.name in ('VERSION','demo_pilot.json')):
                raise DeployError('升级文件不在源码白名单')
            total += m.size
            if total > 30*1024*1024:
                raise DeployError('源码包过大；模型、数据库不能作为升级源码发送')
            out = dest/name
            out.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(m) as src, out.open('wb') as dst:
                shutil.copyfileobj(src,dst)
    manifest=json.loads((dest/'release.json').read_text())
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}',manifest.get('version','')):
        raise DeployError('非法版本号')
    if (dest/'bycall/VERSION').read_text().strip()!=manifest['version']:
        raise DeployError('镜像版本与清单不一致')
    for name,digest in manifest['files'].items():
        p=Path(name)
        if p.is_absolute() or '..' in p.parts or sha(dest/p)!=digest:
            raise DeployError('源码校验失败')
    return manifest


def initialize_env(project, bind_ip):
    import ipaddress
    ipaddress.IPv4Address(bind_ip)
    path=project/'.env'
    if path.exists():
        raise DeployError('.env 已存在，首次初始化不会覆盖')
    creds=json.loads((project/'mosquitto/auth/credentials.json').read_text())
    if not creds.get('host') or not (project/'config/store.json').is_file():
        raise DeployError('请先生成设备账号及门店配置')
    settings=dict(COMPOSE_PROJECT_NAME='bycall',BIND_IP=bind_ip,BYCALL_CONFIG='/config/store.json',
                  MQ_CONF='mosquitto.prod.conf',MQTT_USER='host',MQTT_PASSWORD=creds['host'],
                  BYCALL_API_KEY=secrets.token_hex(32),BACKUP_PATH='./data/backups')
    settings['BYCALL_WATCH_ADMINS']="'"+json.dumps({secrets.token_hex(32):'护士站管理员'},ensure_ascii=False)+"'"
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        f.write('\n'.join(k+'='+v for k,v in settings.items())+'\n')


def wait_health(project, version, seconds=90):
    deadline=time.monotonic()+seconds
    code='import json,os,urllib.request; print(json.load(urllib.request.urlopen("http://127.0.0.1:"+os.getenv("BYCALL_PORT","8000")+"/health",timeout=3))["version"])'
    while time.monotonic()<deadline:
        try:
            cid=compose(project,'ps','-q','host')
            if cid and run(['docker','exec',cid,'python','-c',code],timeout=8)==version:
                return True
        except (DeployError,subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    return False


def snapshot_database(source, target):
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    dst=sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise DeployError('数据库快照完整性检查失败')
    finally:
        src.close();dst.close()
    target.chmod(0o600)


def _apply_locked(project, archive, digest, initialize=False, bind_ip='192.168.1.108'):
    if os.geteuid()!=0:
        raise DeployError('需要在本地终端通过交互 sudo 执行；不要把密码发给助手')
    if platform.machine()!='x86_64':
        raise DeployError('目标不是 x86_64 N100')
    if not (project/'docker-compose.yml').is_file():
        raise DeployError('未找到安装目录')
    if initialize and (project/'.env').exists():
        raise DeployError('已存在 .env，请选升级而不是首次初始化')
    if not initialize and not (project/'.env').exists():
        raise DeployError('首次配置尚未完成，请选初始化')
    if shutil.disk_usage(project).free < 1024**3:
        raise DeployError('可用空间不足 1GB')
    with tempfile.TemporaryDirectory(prefix='bycall-upgrade-') as temp:
        ctx=Path(temp)
        meta=extract_payload(archive,ctx,digest)
        config=json.loads(compose(project,'config','--format','json'))
        image=config['services']['host']['image']
        if not re.fullmatch(r'bycall-host:[A-Za-z0-9_.-]+',image):
            raise DeployError('主机镜像标签不在本项目允许范围')
        cid=compose(project,'ps','-q','host')
        if initialize and cid:
            raise DeployError('已有主机容器，拒绝首次初始化')
        if not initialize and not cid:
            raise DeployError('没有运行中的主机，拒绝猜测升级状态')
        old=inspect(cid) if cid else None
        if old and not old['State']['Running']:
            raise DeployError('原主机未运行，先排查原服务')
        base_id=old['Image'] if old else json.loads(run(['docker','image','inspect',image]))[0]['Id']
        old_version=run(['docker','run','--rm','--pull','never','--network','none',base_id,'python','-c','from bycall.version import VERSION; print(VERSION)'])
        dep_hash=run(['docker','run','--rm','--pull','never','--network','none',base_id,'python','-c','from pathlib import Path; import hashlib; print(hashlib.sha256(Path("/app/requirements.txt").read_bytes()).hexdigest())'])
        if dep_hash!=sha(ctx/'requirements.txt'):
            raise DeployError('依赖已变更，需要在 Mac 重建完整离线镜像；本增量脚本不会联网安装依赖')
        base_tag='bycall-host:base-'+meta['version']
        new_tag='bycall-host:release-'+meta['version']
        run(['docker','tag',base_id,base_tag])
        (ctx/'Dockerfile').write_text('FROM '+base_tag+'\nCOPY bycall /app/bycall\nCOPY tools /app/tools\n')
        print('[1/5] 本地离线增量构建（不下载基础镜像或依赖）',flush=True)
        run(['docker','build','--pull=false','--network=none','-t',new_tag,ctx],timeout=600,capture=False)
        models=project/'models'
        env=config['services']['host'].get('environment',{})
        model_path=env.get('STT_MODEL_DIR') or '/models'
        print('[2/5] 在隔离容器实际加载本地语音模型',flush=True)
        run(['docker','run','--rm','--pull','never','--network','none','--read-only','--tmpfs','/tmp',
             '-v',str(models)+':/models:ro','-e','STT_MODEL_DIR='+model_path,new_tag,
             'python','tools/check_stt_model.py','--load'],timeout=300,capture=False)
        backup=project/'upgrade-backups'/meta['version']
        backup.mkdir(parents=True,mode=0o700,exist_ok=False)
        backup.parent.chmod(0o700)
        (backup/'release.json').write_text(json.dumps(dict(version=meta['version'],old_image=base_id,
                                                        image_tag=image,old_version=old_version),indent=2))
        (backup/'release.json').chmod(0o600)
        try:
            if old:
                data=next((Path(m['Source']) for m in old['Mounts'] if m['Destination']=='/data' and m['Type']=='bind'),None)
                if data is None or not (data/'host.db').is_file():
                    raise DeployError('无法定位当前业务库，升级前拒绝停机')
                print('[3/5] 停止主机并创建本地一致性数据库备份',flush=True)
                compose(project,'stop','-t','20','host',capture=False)
                snapshot_database(data/'host.db',backup/'host.db')
            else:
                print('[3/5] 首次生成配置，密钥只保存在 N100 本机',flush=True)
                initialize_env(project,bind_ip)
            run(['docker','tag',new_tag,image])
            print('[4/5] 使用新镜像启动主机（保留全部现有连接配置和挂载）',flush=True)
            args=['up','-d','--no-build','--pull','never']
            if old:args+=['--no-deps','host']
            compose(project,*args,capture=False)
            if not wait_health(project,meta['version']):
                raise DeployError('新主机健康或版本校验未通过')
            print('[5/5] 升级完成，版本 '+meta['version']+'；模型实际加载验证通过。',flush=True)
            print('数据库快照仅保留在 N100：'+str(backup),flush=True)
        except BaseException as error:
            print('更新失败，保留数据库和错误现场，尝试恢复旧镜像。',flush=True)
            try:
                run(['docker','tag',base_id,image])
                if old:
                    compose(project,'up','-d','--no-deps','--no-build','--pull','never','host',capture=False)
                    recovered=wait_health(project,old_version,60)
                    print('旧镜像恢复健康。' if recovered else '旧镜像健康未确认，需要人工检查；未自动回退数据库。',flush=True)
                else:
                    print('首次初始化失败，配置已保留；请检查后重试，未删除数据。',flush=True)
            except Exception:
                print('恢复旧镜像未完成；请保留现场，人工检查。',flush=True)
            raise DeployError(str(error)) from error


def apply(project, archive, digest, initialize=False, bind_ip='192.168.1.108'):
    if os.geteuid()!=0:
        raise DeployError('需要交互 sudo；不保存密码')
    fd=os.open(project/'.bycall-upgrade.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError('已有升级任务正在执行')
        return _apply_locked(project,archive,digest,initialize,bind_ip)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('check','apply','initialize'))
    p.add_argument('--project',default='/opt/bycall/host')
    p.add_argument('--archive')
    p.add_argument('--sha256')
    p.add_argument('--bind-ip',default='192.168.1.108')
    args=p.parse_args()
    project=Path(args.project).resolve()
    try:
        if args.action=='check':check(project)
        else:
            if not args.archive or not re.fullmatch(r'[a-f0-9]{64}',args.sha256 or ''):
                raise DeployError('缺少升级包或 SHA256')
            apply(project,Path(args.archive).resolve(),args.sha256,args.action=='initialize',args.bind_ip)
    except Exception as e:
        print('未完成：'+str(e),flush=True)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
