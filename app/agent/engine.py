"""
SnoozMate Agent 决策引擎 —— 产品灵魂
核心：五问循环 + 预算管理 + 冷却机制 + 验证反馈 + 七晚学习

不是"检测到就动"的自动化，而是有判断力的 Agent。
"""
import time
import json
from typing import Dict, Optional, List
from dataclasses import dataclass, field


@dataclass
class AgentState:
    """Agent 状态机状态"""
    state: str = "STANDBY"  # STANDBY / MONITORING / CONFIRMING / INTERVENTION / VERIFYING / COOLDOWN
    rounds_done: int = 0
    current_level: int = 0
    intervention_start_time: float = 0
    last_intervention_time: float = 0
    snore_streak_seconds: float = 0    # 连续鼾声累计时长
    bed_time: float = 0                # 入睡时间（用于入睡保护）
    # 验证窗口数据
    verify_start_time: float = 0       # 验证窗口起始时间
    verify_snore_stopped: bool = False
    verify_body_moved: bool = False
    budget_notified: bool = False      # 预算耗尽只提醒一次
    # 七晚学习结果
    weekly_stats: dict = field(default_factory=dict)


class SnoozMateAgent:
    """
    好眠 Agent —— 有分寸感的睡眠守护者

    五问循环（每 1 秒执行一次）：
      1. 人在床上吗？
      2. 是连续周期性鼾声吗？
      3. 冷却期过了吗？
      4. 今晚预算够吗？
      5. 硬件安全吗？

    都通过 → 启动干预（从起始等级开始，渐进升级）
    否则 → 选择不干预，并记录原因
    """

    def __init__(self, config: dict, device_id: str = "dev_001"):
        self.config = config
        self.device_id = device_id
        self.state = AgentState()
        self.decision_log = []  # 决策日志，用于可视化时间轴

    def _log(self, level: str, msg: str, data: dict = None):
        """记录决策过程（供大屏可视化用）"""
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "level": level,      # info / warn / action / success / skip
            "message": msg,
            "data": data or {},
            "state": self.state.state,
            "rounds_done": self.state.rounds_done,
            "rounds_remaining": self.config["max_rounds_per_night"] - self.state.rounds_done,
        }
        self.decision_log.append(entry)
        # 只保留最近 200 条
        if len(self.decision_log) > 200:
            self.decision_log = self.decision_log[-200:]
        return entry

    def process_tick(self, audio_confidence: float, in_bed: bool,
                     body_motion_level: float, temp_ok: bool = True,
                     is_partner: bool = False) -> Dict:
        """
        每 tick 处理一次传感器数据（建议 1Hz）
        返回：{"action": ..., "reason": ..., "level": ..., "log_entry": ...}
        """
        now = time.time()
        cfg = self.config

        # === 第一问：人在床上吗？（雷达门控）===
        if not in_bed:
            if self.state.state != "STANDBY":
                self.state.state = "STANDBY"
                self.state.snore_streak_seconds = 0
                self._log("info", "🛏️  无人在床，进入待机")
            return {"action": "NONE", "reason": "无人在床", "level": 0}

        # 入睡保护：上床后 15 分钟内不干预
        if now - self.state.bed_time < cfg["fall_asleep_protection"]:
            remaining = int(cfg["fall_asleep_protection"] - (now - self.state.bed_time))
            return {"action": "NONE", "reason": f"入睡保护中（剩{remaining}秒）", "level": 0}

        # === 第二问：是连续周期性鼾声吗？===
        if audio_confidence >= cfg["snore_confidence_threshold"]:
            self.state.snore_streak_seconds += 1.0  # 假设 1Hz tick
        else:
            # 衰减：不是连续的话慢慢减
            self.state.snore_streak_seconds = max(0, self.state.snore_streak_seconds - 0.5)

        # 还没到确认时长
        if self.state.snore_streak_seconds < cfg["snore_confirm_seconds"]:
            if self.state.state == "INTERVENTION" or self.state.state == "VERIFYING":
                # 干预中或验证中，继续往下走（验证鼾声是否停止）
                pass
            else:
                # 注意：不能覆盖 COOLDOWN —— 冷却期内保持冷却状态，
                # 否则下面的冷却检查会被绕过，冷却机制失效
                if self.state.state != "COOLDOWN":
                    self.state.state = "MONITORING"
                return {
                    "action": "MONITOR",
                    "reason": f"观察中，连续鼾声 {self.state.snore_streak_seconds:.0f}/{cfg['snore_confirm_seconds']}秒",
                    "level": 0,
                    "confidence": audio_confidence,
                }

        # === 第三问：冷却期过了吗？===
        if self.state.state == "COOLDOWN":
            elapsed = now - self.state.last_intervention_time
            if elapsed < cfg["cooldown_seconds"]:
                remaining = int(cfg["cooldown_seconds"] - elapsed)
                return {"action": "NONE", "reason": f"冷却中（剩{remaining}秒）", "level": 0}
            self.state.state = "MONITORING"
            self._log("info", "⏳ 冷却结束，恢复观察")

        # === 第四问：今晚预算用完了吗？===
        max_rounds = (cfg["partner_mode_max_rounds"] if is_partner
                      else cfg["max_rounds_per_night"])
        if self.state.rounds_done >= max_rounds:
            if not self.state.budget_notified:
                self._log("skip", "🛑 今夜预算用完，选择不干预",
                          {"rounds_done": self.state.rounds_done, "max": max_rounds})
                self.state.budget_notified = True
                self.state.state = "COOLDOWN"  # 进入"终局冷却"
            return {"action": "NONE", "reason": f"今夜预算用完（{self.state.rounds_done}/{max_rounds}轮）",
                    "level": 0}

        # === 第五问：硬件安全吗？===
        if not temp_ok:
            self._log("warn", "🌡️  温度异常，锁定", {"temp_ok": temp_ok})
            return {"action": "NONE", "reason": "温度异常，安全锁定", "level": 0}

        # === 全过 → 启动干预 ===
        if self.state.state not in ("INTERVENTION", "VERIFYING"):
            self.state.state = "INTERVENTION"
            self.state.current_level = cfg["start_vibration_level"]
            self.state.intervention_start_time = now
            self.state.rounds_done += 1
            self.state.budget_notified = False
            self.state.verify_snore_stopped = False
            self.state.verify_body_moved = False
            self._log("action", f"✅ 五问全过 → 第{self.state.rounds_done}轮 L{self.state.current_level} 振动启动",
                      {"round": self.state.rounds_done,
                       "level": self.state.current_level,
                       "remaining": max_rounds - self.state.rounds_done})

        # ── 验证窗口：鼾声已停 → 停止振动，等体动确认 ──
        if self.state.state == "VERIFYING":
            # 鼾声又响了 → 恢复振动（回到干预状态）
            if audio_confidence >= cfg["snore_confidence_threshold"]:
                self.state.state = "INTERVENTION"
                self._log("action", "🔁 验证期间鼾声复发，恢复振动")
                # 落到下面继续走干预升级逻辑
            else:
                verify_elapsed = now - self.state.verify_start_time
                if verify_elapsed >= cfg["verify_window_seconds"]:
                    # 窗口内没有体动 → 按鼾声已停处理，进入冷却
                    self._enter_cooldown("验证窗口超时（鼾声已停，未检测到体动）")
                    return {"action": "STOP", "reason": "验证窗口结束，进入冷却", "level": 0}
                # 验证期间不再振动、不升级
                return {"action": "PAUSE", "reason": f"鼾声已停，验证中（{int(cfg['verify_window_seconds'] - verify_elapsed)}秒）",
                        "level": 0}

        # 干预中：判断是否需要升级
        elapsed_ms = (now - self.state.intervention_start_time) * 1000
        upgrade_steps = int(elapsed_ms / cfg["upgrade_interval_ms"])
        new_level = min(
            cfg["start_vibration_level"] + upgrade_steps,
            max(cfg["start_vibration_level"], cfg.get("max_vibration_level", 3)),
        )

        if new_level > self.state.current_level:
            self.state.current_level = new_level
            self._log("action", f"⬆️ 升级到 L{new_level}",
                      {"level": new_level, "elapsed_ms": int(elapsed_ms)})

        # 单次最长时间保护
        if elapsed_ms >= cfg["max_vibration_duration_ms"]:
            self._enter_cooldown("单次时长超限，停止")
            return {"action": "STOP", "reason": "单次最长时间保护",
                    "level": self.state.current_level}

        return {
            "action": f"VIBRATE_L{self.state.current_level}",
            "reason": f"第{self.state.rounds_done}轮干预中",
            "level": self.state.current_level,
            "elapsed_ms": int(elapsed_ms),
        }

    def report_snore_stopped(self):
        """硬件检测到鼾声停止 → 进入验证窗口"""
        if self.state.state == "INTERVENTION":
            self.state.state = "VERIFYING"
            self.state.verify_start_time = time.time()
            self.state.verify_snore_stopped = True
            self._log("info", "🔇 鼾声停止，停止振动，验证中...")

    def report_body_moved(self):
        """检测到明显体动 → 验证成功"""
        self.state.verify_body_moved = True
        if self.state.state in ("INTERVENTION", "VERIFYING"):
            self._enter_cooldown("检测到体动，干预成功")
            return True
        return False

    def _enter_cooldown(self, reason: str):
        """进入冷却期"""
        self.state.state = "COOLDOWN"
        self.state.last_intervention_time = time.time()
        self.state.snore_streak_seconds = 0
        self._log("success", f"✨ {reason} → 进入冷却",
                  {"round": self.state.rounds_done,
                   "level_used": self.state.current_level})

    def notify_bedtime(self):
        """检测到用户上床 → 重置状态，启动入睡保护"""
        self.state.bed_time = time.time()
        self.state.state = "STANDBY"
        self.state.snore_streak_seconds = 0
        self._log("info", "🛌 检测到上床，进入入睡保护（15分钟）")

    def reset_night(self):
        """新的一夜 → 重置计数"""
        self.state.rounds_done = 0
        self.state.state = "STANDBY"
        self.state.current_level = 0
        self.state.snore_streak_seconds = 0
        self._log("info", "🌙 新的一夜，状态重置")

    def get_status(self) -> dict:
        """获取当前 Agent 状态（供前端展示）"""
        cfg = self.config
        return {
            "state": self.state.state,
            "rounds_done": self.state.rounds_done,
            "rounds_remaining": max(0, cfg["max_rounds_per_night"] - self.state.rounds_done),
            "current_level": self.state.current_level,
            "snore_streak": round(self.state.snore_streak_seconds, 1),
            "decisions": self.decision_log[-50:],  # 最近 50 条
        }


# ============================================================
# 七晚学习 Agent（云端专属，本地不做）
# ============================================================

def weekly_adjustment(weekly_events: List[dict], current_config: dict) -> dict:
    """
    七晚学习：根据一周数据自动调整 Agent 参数

    规则（简单可解释，不用LLM）：
    - 成功率 > 80% → 降低起始强度 + 加长冷却（更温和）
    - 成功率 < 50% → 提高起始强度 + 缩短冷却（更果断）
    - 每晚平均 > 5 轮 → 提高上限（更多机会）
    - 每晚平均 < 1 轮 → 降低阈值，更早介入
    """
    if not weekly_events:
        return dict(current_config)

    # 统计
    intervention_events = [e for e in weekly_events if e.get("event_type") == "intervention"]
    total_rounds = len(intervention_events)
    success_rounds = len([e for e in intervention_events if e.get("result") == "success"])
    success_rate = success_rounds / total_rounds if total_rounds > 0 else 0

    nights = len(set(e.get("timestamp", 0) // 86400 for e in intervention_events)) or 1
    avg_rounds = total_rounds / nights

    new_cfg = dict(current_config)

    # 成功率高 → 更温和
    if success_rate > 0.8:
        new_cfg["start_vibration_level"] = max(1, new_cfg.get("start_vibration_level", 2) - 1)
        new_cfg["cooldown_seconds"] = min(1200, new_cfg.get("cooldown_seconds", 600) + 120)
    # 成功率低 → 更果断
    elif success_rate < 0.5 and total_rounds >= 3:
        new_cfg["start_vibration_level"] = min(2, new_cfg.get("start_vibration_level", 1) + 1)
        new_cfg["cooldown_seconds"] = max(300, new_cfg.get("cooldown_seconds", 600) - 120)

    # 轮数多 → 提高上限
    if avg_rounds > 5:
        new_cfg["max_rounds_per_night"] = min(10, new_cfg.get("max_rounds_per_night", 6) + 2)

    return {
        "new_config": new_cfg,
        "stats": {
            "total_rounds": total_rounds,
            "success_rounds": success_rounds,
            "success_rate": round(success_rate, 3),
            "nights": nights,
            "avg_rounds_per_night": round(avg_rounds, 1),
        },
        "changes": {k: v for k, v in new_cfg.items() if v != current_config.get(k)}
    }


def generate_morning_report_prompt(events: List[dict], stats: dict) -> str:
    """生成晨报的 LLM prompt"""
    total = stats.get("total_rounds", 0)
    success = stats.get("success_rounds", 0)
    rate = stats.get("success_rate", 0)
    avg_rt = stats.get("avg_response_time", 0)
    max_lvl = stats.get("max_level", 0)

    return f"""你是好眠SnoozMate的睡眠管家。请用温暖、安心的语气，总结用户昨晚的睡眠情况。
重点：不说吓人的话、不用医学术语、不做诊断。
数据只是记录，不说"你有问题"，只说"我们记录到这些"。

昨夜数据：
- 干预次数：{total} 次
- 干预成功：{success} 次（成功率 {int(rate*100)}%）
- 平均响应时间：{avg_rt:.0f} 秒
- 最高振动等级：L{max_lvl}

输出格式（就两句话）：
第一行：昨夜整体总结（30字以内，不用数字堆砌）
第二行：一个小建议（20字以内，温和具体）
"""
