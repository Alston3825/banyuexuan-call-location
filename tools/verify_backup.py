#!/usr/bin/env python3
"""在恢复的独立镜像仓库检查所有已登记源文件。"""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'docs/源码文件校验清单.json').read_text())
failed=[]
for name,digest in manifest.items():
    p=root/name
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
        failed.append(name)
if failed:
    raise SystemExit('校验失败：'+', '.join(failed))
print(f'校验通过：{len(manifest)}个源文件；自动生成快照/清单不包含自身哈希。')
