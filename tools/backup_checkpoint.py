#!/usr/bin/env python3
"""复用独立镜像与同一把锁；不修改开发工作树Git历史或发布服务。"""
import argparse, datetime, fcntl, hashlib, importlib.util, json, os
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[1]
BASE = Path.home() / '.banyuexuan-project-sync'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    now = datetime.datetime.now(ZoneInfo('America/Los_Angeles'))
    with (BASE / 'sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sources = ['docs/AI接手说明.md', 'docs/当前开发任务.md', 'docs/服务器对接状态.md',
                   'development/bycall/BASELINE.md', 'development/bycall/VALIDATION.md',
                   'development/bycall/docs/19-护士站文字与3D双屏同源设计.md']
        evidence = {}
        for name in sources:
            evidence[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        version = (ROOT / 'development/bycall/host/bycall/VERSION').read_text().strip()
        note = '# 自动交接索引\n\n'
        note += f'整理时间：{now.isoformat(timespec="seconds")}（America/Los_Angeles）。\n\n'
        note += f'本地代码版本：`{version}`。这不是现场部署版本或测试通过证明。\n\n'
        note += '先读[交接说明](交接说明.md)，再按下面入口阅读全文。开发进展、测试证据与阻塞以对应文件的最新条目为准，历史条目不能覆盖较新记录。\n\n'
        note += ''.join(f'- [{name}](../{name})；SHA256 `{digest}`\n' for name, digest in evidence.items())
        note += '\n生成索引只证明文件已读取；成功上传须查看独立镜像远端HEAD和本机检查点状态。\n'
        (ROOT / '备份与交接/自动交接索引.md').write_text(note)
        if args.prepare_only:
            print('交接索引已生成；未上传')
            return
        spec = importlib.util.spec_from_file_location('source_sync', BASE / 'sync.py')
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        projects = json.loads((BASE / 'projects.json').read_text())
        project = next(p for p in projects if p['id'] == 'call')
        if Path(project['source']).resolve() != ROOT:
            raise RuntimeError('权威目录与同步配置不一致，停止')
        result = sync.snapshot(project)
        mirror = BASE / 'repos/call'
        remote = sync.git(mirror, 'ls-remote', 'origin', 'refs/heads/main').split()[0]
        if remote != result['commit']:
            raise RuntimeError('远端HEAD不一致，停止登记成功')
        tag = 'checkpoint/' + now.strftime('%Y%m%d-%H%M%S')
        sync.git(mirror, 'tag', tag, result['commit'])
        sync.git(mirror, 'push', 'origin', 'refs/tags/' + tag)
        result.update(checkpoint=tag, timezone='America/Los_Angeles')
        target = BASE / 'call-checkpoint-status.json'
        temp = target.with_suffix('.part')
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        os.replace(temp, target)
        print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    main()
