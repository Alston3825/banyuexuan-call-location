"""检查并可实际加载本地模型，不写业务库，不联网。"""
import argparse
import json
import tempfile
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bycall.core.transcribe import Transcriber


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-dir', default=None)
    parser.add_argument('--load', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='stt-check-') as tmp:
        tr = Transcriber(Path(tmp) / 'check.db', model_dir=args.model_dir)
        try:
            result = tr.load_model() if args.load else tr.status()
        except Exception as e:
            result = dict(available=False, loaded=False, error=str(e))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('available') and (not args.load or result.get('loaded')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
