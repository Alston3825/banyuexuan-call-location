"""只查本地 CTranslate2 模型，禁止以缺失文件触发联网下载。"""
import json
from pathlib import Path

REQUIRED = ('model.bin', 'config.json', 'tokenizer.json')


class ModelError(ValueError):
    pass


def resolve_model(path):
    root = Path(path)
    if not root.is_dir():
        raise ModelError('未找到本地模型目录；请把模型放在 host/models 下')
    if (root / 'model.bin').is_file():
        candidates = [root]
    else:
        candidates = sorted({p.parent for pattern in ('*/model.bin', '*/snapshots/*/model.bin') for p in root.glob(pattern)})
    if not candidates:
        raise ModelError('未找到 CTranslate2 model.bin；.pt 或 pytorch_model.bin 不能直接用于 faster-whisper')
    if len(candidates) > 1:
        raise ModelError('发现多个模型，请用 STT_MODEL_DIR 指定容器内的模型目录')
    model = candidates[0]
    missing = [name for name in REQUIRED if not (model / name).is_file()]
    if missing:
        raise ModelError('模型文件不完整，缺少：' + '、'.join(missing))
    for name in REQUIRED:
        p = model / name
        if p.stat().st_size == 0:
            raise ModelError('模型文件为空：' + name)
        with p.open('rb') as f:
            if f.read(150).startswith(b'version https://git-lfs.github.com/spec/'):
                raise ModelError('下载到的是 Git LFS 指针而非真实模型：' + name)
    for name in ('config.json', 'tokenizer.json'):
        try:
            with (model / name).open(encoding='utf-8') as f:
                value = json.load(f)
            if not isinstance(value, dict):
                raise ValueError()
        except (OSError, ValueError):
            raise ModelError('模型 JSON 无效：' + name)
    return str(model)
