"""Run with python3 -m prototype.demo; uses fictitious data only."""
from pathlib import Path
from tempfile import TemporaryDirectory
from .core import Core


def main():
    with TemporaryDirectory() as folder:
        core = Core(Path(folder) / 'demo.sqlite3')
        call = core.create(store='test-store', device='test-button-A', event_id='press-1',
                           room='测试房A', priority='normal',
                           snapshot={'staff': 'test-nurse-1', 'shift': 'test-day'})
        for version, action, extras in [(1, 'claim', {}),
                                         (2, 'arrive', {'method': 'manual'}),
                                         (3, 'complete', {'method': 'onsite', 'reason': '测试完成'})]:
            core.act(call, store='test-store', actor='test-nurse-1',
                     expected_version=version, action=action, **extras)
        for event in core.events():
            print(event['version'], event['kind'], event['payload'])
        print('演示完成。未连接真实设备、语音、定位或管理系统。')


if __name__ == '__main__':
    main()
