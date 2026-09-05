"""
Pydantic 数据模型定义（API 协议）
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class EventIn(BaseModel):
    """硬件上报事件的输入格式（对应 night_event_v1）
    单条上报时 device_id 必填；批量上报时从 batch 级继承，单条可省略。"""
    device_id: Optional[str] = Field(default="", max_length=128)
    timestamp: Optional[int] = None  # 不填就用服务端时间
    night_id: Optional[str] = Field(default="", max_length=256)
    event_type: str = Field(min_length=1, max_length=64)
    snore_duration_sec: float = Field(default=0, ge=0, le=12 * 60 * 60)
    snore_confidence: float = Field(default=0, ge=0, le=1)
    in_bed: bool = True
    body_motion_level: float = Field(default=0, ge=0, le=1)
    vibration_level: int = Field(default=0, ge=0, le=5)
    vibration_duration_ms: int = Field(default=0, ge=0, le=300000)
    result: str = Field(default="", max_length=64)
    response_time_sec: float = Field(default=0, ge=0, le=3600)
    round_in_night: int = Field(default=0, ge=0, le=100)
    model_version: str = Field(default="rule_v1", max_length=128)
    error_code: int = 0
    note: str = Field(default="", max_length=1000)


class EventOut(EventIn):
    id: int


class DeviceConfig(BaseModel):
    """Agent 配置"""
    max_rounds_per_night: int = 6
    cooldown_seconds: int = 600
    fall_asleep_protection: int = 900
    max_vibration_duration_ms: int = 120000
    start_vibration_level: int = 1
    max_vibration_level: int = 3
    upgrade_interval_ms: int = 8000
    snore_confirm_seconds: int = 10
    snore_confidence_threshold: float = 0.65
    verify_window_seconds: int = 15
    partner_mode_max_rounds: int = 4


class AgentTick(BaseModel):
    """实时 Agent 决策输入（给调试/演示用）"""
    device_id: str
    audio_confidence: float = Field(ge=0, le=1)
    in_bed: bool = True
    body_motion_level: float = Field(default=0, ge=0, le=1)
    temp_ok: bool = True
    is_partner: bool = False


class AgentDecision(BaseModel):
    action: str
    reason: str
    level: int
    state: str
    rounds_done: int
    rounds_remaining: int
    snore_streak: float
    recent_log: List[Dict[str, Any]]


class MorningReport(BaseModel):
    device_id: str
    date: str
    total_rounds: int
    success_rounds: int
    success_rate: float
    avg_response_time: float
    max_vibration_level: int
    report_text: str
    events_count: int


class WeeklyStats(BaseModel):
    device_id: str
    nights: int
    total_rounds: int
    success_rate: float
    avg_rounds_per_night: float
    param_changes: Dict[str, Any]
    new_config: Dict[str, Any]
