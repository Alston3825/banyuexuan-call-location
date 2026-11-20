"""Message protocol (dev plan §四). All devices speak this envelope over MQTT."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# message types
CALL_REQUEST = 'CALL_REQUEST'      # room -> host
CALL_DISPATCH = 'CALL_DISPATCH'    # host -> watch / station
CALL_TAKEN = 'CALL_TAKEN'          # host -> other notified watches
CALL_STATE = 'CALL_STATE'          # host -> owner watch (arrived / completed)
CALL_ACCEPT = 'CALL_ACCEPT'        # watch -> host
CALL_ARRIVE = 'CALL_ARRIVE'        # watch -> host (manual arrival)
CALL_COMPLETE = 'CALL_COMPLETE'    # watch -> host
CALL_CANCEL = 'CALL_CANCEL'        # room/watch -> host
CALL_TRANSFER = 'CALL_TRANSFER'    # watch -> host
CALL_FEEDBACK = 'CALL_FEEDBACK'    # host -> room tablet
CMD_REJECTED = 'CMD_REJECTED'      # host -> sender
HEARTBEAT = 'HEARTBEAT'
LOCATION_REPORT = 'LOCATION_REPORT'
VITALS_REPORT = 'VITALS_REPORT'    # watch -> host (heart rate etc.), local only
DEVICE_CONFIG = 'DEVICE_CONFIG'  # host -> room tablet (button list), on topic reply/{dev}
STATUS = 'STATUS'                  # LWT / online marker
# ---- 2026-11-17:回执、一键求助、同事沟通、换表 ----
CALL_RECEIVED = 'CALL_RECEIVED'    # watch -> host on ack/{call}: 已收到这条派单(主机据此停止重发、统计送达时间)
HELP_REQUEST = 'HELP_REQUEST'      # watch -> host on up/{w}: 一键求助
HELP_STATE = 'HELP_STATE'          # host -> 求助人手表: sent / accepted / escalated / arrived / completed / cancelled
CHAT_SEND = 'CHAT_SEND'            # watch -> host on up/{w}: 发同事消息(快捷回复/文字/语音)
CHAT_MSG = 'CHAT_MSG'              # host -> watch: 一条同事消息
CHAT_ACK = 'CHAT_ACK'              # watch -> host on up/{w}: 已收到这些消息 {ids:[...]}
WATCH_BIND = 'WATCH_BIND'          # host -> watch on reply/{w}: 这块表当前绑定谁(staff 为空 = 未绑定/已换出) + 手表参数
WATCH_RESET = 'WATCH_RESET'        # host -> watch: 归还备用池,清空本地个人数据
RESET_DONE = 'RESET_DONE'          # watch -> host on up/{w}: 已清空
RETURN_HOME = 'RETURN_HOME'        # watch -> host on up/{w}: 一键归位(切回原表)
APP_UPDATE = 'APP_UPDATE'          # host -> 平板/手表 on reply/{dev}: 有新版本了,马上检查更新

FEEDBACK_TEXT = {
    'received': '已接到呼叫，请稍后',
    'accepted': '{nurse}已接单，马上过来',
    'arrived': '护理师已到达',
    'completed': '已完成',
    'cancelled': '呼叫已取消',
}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


class Envelope(BaseModel):
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    call_id: Optional[str] = None
    store_id: str
    device_id: str
    timestamp: str = Field(default_factory=_now_iso)
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator('store_id', 'device_id', 'msg_id', 'type')
    @classmethod
    def _nonempty(cls, v):
        if not v or not v.strip():
            raise ValueError('must be non-empty')
        return v


class Reading(BaseModel):
    beacon_id: str = Field(min_length=1, max_length=80)
    rssi: int = Field(ge=-127, le=0)
    sample_count: Optional[int] = Field(default=None, ge=1, le=100000)
    age_ms: Optional[int] = Field(default=None, ge=0, le=3600000)


class LocationPayload(BaseModel):
    readings: List[Reading] = Field(max_length=128)
    sampled_at: Optional[datetime] = None
    report_interval_s: Optional[int] = Field(default=None, ge=7, le=60)
    scan_status: Optional[str] = Field(default=None, max_length=40)
    scan_error_code: Optional[int] = Field(default=None, ge=0, le=100)
    observation_source: Optional[str] = Field(default=None, pattern='^(ble|simulated)$')


class VitalsPayload(BaseModel):
    heart_rate: Optional[int] = Field(default=None, ge=20, le=250)
    spo2: Optional[int] = Field(default=None, ge=50, le=100)
    temperature: Optional[float] = Field(default=None, ge=30, le=45)
    steps: Optional[int] = Field(default=None, ge=0)
    measured_at: Optional[str] = None


class RequestPayload(BaseModel):
    room: str                     # room_id (management-system cuid)
    service_code: str             # e.g. NEED_NURSE / EMERGENCY, see service-codes
    priority: str = 'normal'
    voice_url: Optional[str] = None
    text: Optional[str] = None


class HeartbeatPayload(BaseModel):
    battery: Optional[int] = Field(default=None, ge=0, le=100)
    wifi_rssi: Optional[int] = None
    charging: Optional[bool] = None    # watches: currently on the charger
    cfg_ver: Optional[str] = None      # tablets: version of the button list they hold
    caps: Optional[List[str]] = None   # watches: 支持的能力,如 receipt/help/chat/bind(老版本 APK 不带,主机就不等回执)
    staff: Optional[str] = None        # watches: 手表认为自己绑定的是谁;与主机不一致时主机重发 WATCH_BIND


class ChatSendPayload(BaseModel):
    channel: Optional[str] = None      # floor:16 / dept / all / dm:<staff> / role:<role>
    kind: str = 'text'                 # quick / text / voice
    text: Optional[str] = Field(default=None, max_length=200)
    voice_url: Optional[str] = None
    duration_s: Optional[float] = Field(default=None, ge=0, le=600)
    client_id: Optional[str] = None    # 手表本地编号,用于把"发送中"换成"已发出"


# topics --------------------------------------------------------------
def t_call(store, floor, room): return f'{store}/call/{floor}/{room}'
def t_dispatch_watch(store, watch): return f'{store}/dispatch/{watch}'
def t_dispatch_station(store): return f'{store}/station/dispatch'
def t_ack(store, call_id): return f'{store}/ack/{call_id}'
def t_feedback(store, room): return f'{store}/feedback/{room}'
def t_heartbeat(store, dev): return f'{store}/heartbeat/{dev}'
def t_location(store, watch): return f'{store}/location/{watch}'
def t_vitals(store, watch): return f'{store}/vitals/{watch}'
def t_status(store, dev): return f'{store}/status/{dev}'
def t_events(store): return f'{store}/call/events'
def t_reply(store, dev): return f'{store}/reply/{dev}'
def t_up(store, dev): return f'{store}/up/{dev}'        # 手表上行:求助 / 同事消息 / 一键归位 / 重置完成


def host_subscriptions(store):
    return [f'{store}/call/+/+', f'{store}/ack/+', f'{store}/heartbeat/+',
            f'{store}/location/+', f'{store}/vitals/+', f'{store}/status/+', f'{store}/up/+']
