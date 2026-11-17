#!/usr/bin/env python3
"""Mac / Windows 共用的 N100 调试发布入口：只传白名单源码，N100 基于本地镜像离线增量构建。"""
import argparse
import hashlib
import io
import ipaddress
import json
import re
import shlex
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def run(args, **kw):
    r=subprocess.run([str(x) for x in args],**kw)
    if r.returncode:
        raise RuntimeError('步骤未完成：'+str(args[0]))
    return r


def package(root, output):
    files={}
    for folder in ('bycall','tools'):
        for p in (root/'host'/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and (p.suffix in ('.py','.html') or p.name in ('VERSION','demo_pilot.json')):
                if p.is_symlink():raise RuntimeError('源码包不接受符号链接：'+str(p))
                files[str(p.relative_to(root/'host')).replace('\\','/')]=p.read_bytes()
    files['requirements.txt']=(root/'host/requirements.txt').read_bytes()
    fingerprint=hashlib.sha256(b''.join(k.encode()+files[k] for k in sorted(files))).hexdigest()[:8]
    version=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+fingerprint
    files['bycall/VERSION']=(version+'\n').encode()
    manifest=dict(version=version,files={n:hashlib.sha256(b).hexdigest() for n,b in files.items()})
    files['release.json']=json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode()
    output.mkdir(parents=True,exist_ok=True)
    archive=output/(version+'.tar.gz')
    with tarfile.open(archive,'w:gz') as tar:
        for name,data in sorted(files.items()):
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644
            tar.addfile(info,io.BytesIO(data))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix+'.sha256').write_text(digest+'  '+archive.name+'\n')
    return archive,manifest,digest


def ssh_options(args, binary='ssh'):
    cmd=[binary,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8']
    if args.identity:
        identity=Path(args.identity).expanduser()
        if not identity.is_file():raise RuntimeError('找不到 SSH 身份文件，请先完成 SSH 授权')
        cmd+=['-i',identity,'-o','IdentitiesOnly=yes']
    return cmd


def readonly_check(args):
    # 在标准输入执行源码，不上传任何文件。
    worker=(ROOT/'host/deploy/n100_remote.py').read_text()
    cmd=ssh_options(args)+[args.target,shlex.join(['python3','-','check','--project',args.project])]
    run(cmd,input=worker,text=True)


def menu(args):
    print('1. 只读检查 SSH、模型和安装状态\n2. 打包源码（不连接 N100）\n3. 首次配置并启动测试主机\n4. 升级已运行的主机/大屏')
    choice=input('请选择 [默认 1]：').strip() or '1'
    actions={'1':'check','2':'package','3':'initialize','4':'apply'}
    if choice not in actions:raise RuntimeError('无效选择')
    args.action=actions[choice]
    if args.action in ('initialize','apply'):
        word='测试初始化' if args.action=='initialize' else '上线'
        args.confirm=input('本操作会'+('首次生成配置并启动测试服务' if args.action=='initialize' else '备份并短暂停止主机进行升级')+'；输入“'+word+'”继续：').strip()
    return args


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',nargs='?',choices=('check','package','initialize','apply','menu'),default='menu')
    p.add_argument('--host',default='192.168.1.108')
    p.add_argument('--user',default='gateway-byx')
    p.add_argument('--identity',default=str(Path.home()/'.ssh/banyuexuan_deploy') if (Path.home()/'.ssh/banyuexuan_deploy').is_file() else None)
    p.add_argument('--project',default='/opt/bycall/host')
    p.add_argument('--confirm',default='')
    args=p.parse_args()
    try:
        ipaddress.IPv4Address(args.host)
        if not re.fullmatch(r'[a-z_][a-z0-9_-]*',args.user):raise RuntimeError('无效 SSH 用户名')
        if not Path(args.project).is_absolute():raise RuntimeError('安装目录必须是绝对路径')
        args.target=args.user+'@'+args.host
        if args.action=='menu':args=menu(args)
        if args.action=='check':readonly_check(args);return 0
        if args.action in ('apply','initialize') and args.confirm!=('上线' if args.action=='apply' else '测试初始化'):
            raise RuntimeError('尚未确认执行；可先选只读检查或打包')
        archive,meta,digest=package(ROOT,ROOT/'dist/n100-releases')
        print('源码包：'+str(archive)+'（'+str(archive.stat().st_size)+' 字节；不含模型、密码或数据库）',flush=True)
        if args.action=='package':return 0
        # 先检查 SSH，再在用户缓存中暂存；从不关闭主机密钥核验。
        readonly_check(args)
        remote='.cache/bycall-deploy/'+meta['version']
        mkdir='from pathlib import Path; Path('+repr(remote)+').mkdir(parents=True,exist_ok=True,mode=0o700)'
        run(ssh_options(args)+[args.target,shlex.join(['python3','-c',mkdir])])
        worker=ROOT/'host/deploy/n100_remote.py'
        run(ssh_options(args,'scp')+[archive,worker,args.target+':'+remote+'/'])
        remote_archive=remote+'/'+archive.name
        remote_worker=remote+'/'+worker.name
        cmd=['sudo','--','python3',remote_worker,args.action,'--project',args.project,
             '--archive',remote_archive,'--sha256',digest,'--bind-ip',args.host]
        print('已传输完成。若终端询问 sudo 密码，仅在终端输入 N100 登录密码；脚本不保存密码。',flush=True)
        run(ssh_options(args)+['-tt',args.target,shlex.join(cmd)])
        print('完成。大屏地址：http://'+args.host+':8100/station；刷新页面即可使用新大屏。')
        return 0
    except (RuntimeError,ValueError,OSError,KeyboardInterrupt) as e:
        print('未完成：'+str(e),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
