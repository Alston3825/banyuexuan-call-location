"""Unified admin routes; configuration contents are never served without host admin auth."""
import os
import re

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core.terminal_config import SCHEMAS, ConfigConflict, ConfigError


class Save(BaseModel):
    model_config = ConfigDict(extra='forbid')
    targets: list[str] = Field(min_length=1, max_length=300)
    settings: dict
    expected: dict[str, int]


class Ack(BaseModel):
    revision: int
    status: str


class BeaconSave(BaseModel):
    items: list[dict]
    expected: int


def install(app, svc, auth):
    configs = svc.terminal_configs

    def station_id(device):
        if not re.fullmatch(r'station-[A-Za-z0-9_-]{1,64}', device):
            raise HTTPException(400, '大屏编号格式无效')

    def known(kind):
        if kind == 'watch':
            return {w['sn']: w.get('bound_staff') or w['sn'] for w in svc.watches.all()}
        if kind == 'tablet':
            return {v['tablet']: str(r) + '房平板' for r, v in svc.cfg.rooms.items() if v.get('tablet')}
        if kind == 'station':
            return {d: d for d in configs.station_ids()}
        raise HTTPException(404, '终端类型不存在')

    def errors(fn):
        try:
            return fn()
        except ConfigConflict as e:
            raise HTTPException(409, str(e))
        except ConfigError as e:
            raise HTTPException(400, str(e))

    @app.get('/admin/terminals')
    def page():
        return FileResponse(os.path.join(os.path.dirname(__file__), 'terminals.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/admin/terminal-config', dependencies=[Depends(auth)])
    def listing():
        devices = []
        for kind in SCHEMAS:
            for dev, name in known(kind).items():
                devices.append(dict(configs.get(kind, dev), name=name))
        return dict(schema=SCHEMAS, devices=devices, rooms=list(svc.cfg.rooms), store=svc.cfg.store_id)

    @app.put('/admin/terminal-config/{kind}', dependencies=[Depends(auth)])
    def save(kind: str, body: Save):
        ids = known(kind)
        if any(d not in ids for d in body.targets):
            raise HTTPException(404, '所选终端未登记')
        rev = errors(lambda: configs.save(kind, body.targets, body.settings, body.expected))
        for dev in body.targets:
            if kind != 'station':
                svc.push_terminal_config(dev)
        return dict(revision=rev, status='pending', count=len(body.targets))

    @app.get('/admin/beacons', dependencies=[Depends(auth)])
    def beacons():
        out = configs.beacons()
        if out['mapping'] is None:
            out['mapping'] = svc.cfg.beacons
        return out

    @app.put('/admin/beacons', dependencies=[Depends(auth)])
    def save_beacons(body: BeaconSave):
        out = errors(lambda: configs.save_beacons(body.items, svc.cfg.rooms, body.expected))
        svc.cfg.beacons = out['mapping']
        svc.locator.beacons = out['mapping']
        svc.locator._hist.clear()
        svc.locator.current.clear()
        return out

    # Browser screens use authenticated long polling: a saved revision wakes them immediately.
    @app.get('/terminal-config/station/{device}', dependencies=[Depends(auth)])
    def station_config(device: str, after: int = -1):
        station_id(device)
        configs.seen('station', device, True)
        with configs.changed:
            if configs.get('station', device)['revision'] == after:
                configs.changed.wait(timeout=20)
        return configs.get('station', device)

    @app.post('/terminal-config/station/{device}/ack', dependencies=[Depends(auth)])
    def station_ack(device: str, body: Ack):
        station_id(device)
        ok = errors(lambda: configs.ack('station', device, body.revision, body.status))
        if not ok:
            raise HTTPException(409, '配置版本已变更，请重新获取')
        return dict(ok=True)
