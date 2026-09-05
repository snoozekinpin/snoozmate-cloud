"""
候选配置管理 —— 用户确认后才允许应用建议
对应时序图第 ⑤ 阶段
"""
import json
import uuid
from datetime import datetime
from app.database import (
    get_conn, get_device_config, update_device_config, get_device,
    gen_id,
)
from app.agent.engine import weekly_adjustment


def create_candidate(device_id: str, suggested_config: dict,
                     source: str = "ai", summary: str = "",
                     basis: list = None, expected_version: int = 0) -> str:
    """创建一个候选配置，返回 candidate_id"""
    conn = get_conn()
    c = conn.cursor()
    candidate_id = gen_id("cand")
    now = int(datetime.now().timestamp())

    # 如果没指定期望版本，用当前版本
    if expected_version == 0:
        device = get_device(device_id)
        expected_version = device["config_version"] if device else 1

    c.execute('''INSERT INTO config_candidates
                 (candidate_id, device_id, suggested_config, expected_config_version,
                  source, summary, basis, status, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?)''',
              (candidate_id, device_id, json.dumps(suggested_config),
               expected_version, source, summary,
               json.dumps(basis or []), "pending", now))
    conn.commit()
    conn.close()
    return candidate_id


def list_candidates(device_id: str, status: str = None, limit: int = 20) -> list:
    """列出候选配置"""
    conn = get_conn()
    c = conn.cursor()
    if status:
        c.execute('''SELECT * FROM config_candidates WHERE device_id=? AND status=?
                     ORDER BY created_at DESC LIMIT ?''', (device_id, status, limit))
    else:
        c.execute('''SELECT * FROM config_candidates WHERE device_id=?
                     ORDER BY created_at DESC LIMIT ?''', (device_id, limit))
    rows = []
    for r in c.fetchall():
        d = dict(r)
        d["suggested_config"] = json.loads(d["suggested_config"]) if d["suggested_config"] else {}
        d["basis"] = json.loads(d["basis"]) if d["basis"] else []
        rows.append(d)
    conn.close()
    return rows


def get_candidate(candidate_id: str) -> dict:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM config_candidates WHERE candidate_id=?", (candidate_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {}
    d = dict(row)
    d["suggested_config"] = json.loads(d["suggested_config"]) if d["suggested_config"] else {}
    d["basis"] = json.loads(d["basis"]) if d["basis"] else []
    return d


def approve_candidate(candidate_id: str, idempotency_key: str = None) -> dict:
    """
    用户确认候选配置 → 应用到设备
    支持幂等键：同一个 idempotency_key 重复调用返回相同结果
    """
    from app.config import DEFAULT_AGENT_CONFIG
    conn = get_conn()
    now, log_idemp = int(datetime.now().timestamp()), idempotency_key or gen_id("idem")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM application_logs WHERE idempotency_key=?", (log_idemp,)).fetchone()
        if existing:
            return {"status": existing["status"], "candidate_id": existing["candidate_id"],
                    "idempotency_key": log_idemp, "already_applied": True}
        cand = conn.execute("SELECT * FROM config_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not cand:
            conn.rollback()
            return {"status": "failed", "error": "candidate not found"}
        cand = dict(cand)
        if cand["status"] == "applied":
            conn.rollback()
            return {"status": "applied", "candidate_id": candidate_id, "already_applied": True, "new_config_version": None}
        if cand["status"] != "pending":
            conn.rollback()
            return {"status": "failed", "error": f"candidate is {cand['status']}"}
        device = conn.execute("SELECT agent_config, config_version FROM devices WHERE device_id=?", (cand["device_id"],)).fetchone()
        if not device or cand["expected_config_version"] != device["config_version"]:
            conn.rollback()
            return {"status": "failed", "error": "config version conflict", "idempotency_key": log_idemp}
        suggested = json.loads(cand["suggested_config"])
        limits = {"max_rounds_per_night": (0, 15), "max_vibration_duration_ms": (0, 300000),
                  "start_vibration_level": (1, 3), "max_vibration_level": (1, 5),
                  "cooldown_seconds": (60, 1800)}
        for key, (low, high) in limits.items():
            if key in suggested and (not isinstance(suggested[key], (int, float)) or isinstance(suggested[key], bool)
                                     or not low <= suggested[key] <= high):
                conn.rollback()
                return {"status": "failed", "error": f"unsafe config: {key}", "idempotency_key": log_idemp}
        current = dict(DEFAULT_AGENT_CONFIG)
        try:
            current.update(json.loads(device["agent_config"]) if device["agent_config"] else {})
        except (TypeError, ValueError):
            pass
        merged, new_version = {**current, **suggested}, int(device["config_version"]) + 1
        conn.execute("UPDATE devices SET agent_config=?, config_version=? WHERE device_id=?",
                     (json.dumps(merged, ensure_ascii=False), new_version, cand["device_id"]))
        conn.execute("UPDATE config_candidates SET status='applied', reviewed_at=?, applied_at=? WHERE candidate_id=?",
                     (now, now, candidate_id))
        conn.execute("""INSERT INTO application_logs(idempotency_key,device_id,candidate_id,config_before,config_after,status,created_at,completed_at)
                        VALUES(?,?,?,?,?,?,?,?)""", (log_idemp, cand["device_id"], candidate_id,
                        json.dumps(current), json.dumps(merged), "applied", now, now))
        conn.commit()
        return {"status": "applied", "candidate_id": candidate_id, "new_config_version": new_version,
                "new_config": merged, "idempotency_key": log_idemp}
    except Exception:
        conn.rollback()
        return {"status": "failed", "error": "candidate apply failed", "idempotency_key": log_idemp}
    finally:
        conn.close()


def reject_candidate(candidate_id: str) -> dict:
    """用户拒绝候选配置"""
    conn = get_conn()
    c = conn.cursor()
    now = int(datetime.now().timestamp())
    c.execute('''UPDATE config_candidates SET status='rejected', reviewed_at=?
                 WHERE candidate_id=? AND status='pending' ''',
              (now, candidate_id))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return {"status": "rejected" if affected > 0 else "not_found"}


def generate_ai_candidate(device_id: str, weekly_stats: dict, current_config: dict) -> str:
    """
    根据七晚统计，自动生成一个 AI 建议的候选配置
    （规则版，不依赖 LLM；LLM 版会补充更自然的 summary 文案）
    """
    result = weekly_adjustment([], current_config)  # 用当前配置当基准
    # 用 weekly_stats 的成功率来驱动调整（简化版）
    new_cfg = dict(current_config)
    basis = []

    rate = weekly_stats.get("success_rate", 0.5)
    avg_rounds = weekly_stats.get("avg_rounds_per_night", 3)
    trend = weekly_stats.get("trend", "stable")

    if rate > 0.8 and trend == "improving":
        # 效果好 → 更温和
        new_cfg["start_vibration_level"] = max(1, new_cfg.get("start_vibration_level", 2) - 1)
        new_cfg["cooldown_seconds"] = min(1200, new_cfg.get("cooldown_seconds", 600) + 120)
        summary = "效果越来越好，建议更温和一些"
        basis = [f"近7晚成功率 {int(rate*100)}%（>80%）", "趋势：改善中"]
    elif rate < 0.5 and avg_rounds >= 3:
        # 效果差 → 更果断
        new_cfg["start_vibration_level"] = min(2, new_cfg.get("start_vibration_level", 1) + 1)
        new_cfg["cooldown_seconds"] = max(300, new_cfg.get("cooldown_seconds", 600) - 120)
        summary = "干预效果偏弱，建议提高起始强度"
        basis = [f"近7晚成功率 {int(rate*100)}%（<50%）", f"平均每晚 {avg_rounds} 轮"]
    else:
        summary = "当前参数合适，建议保持"
        basis = [f"成功率 {int(rate*100)}%", f"趋势：{trend}"]

    candidate_id = create_candidate(
        device_id=device_id,
        suggested_config=new_cfg,
        source="ai",
        summary=summary,
        basis=basis,
    )
    return candidate_id


def get_application_log(device_id: str, limit: int = 10) -> list:
    """获取配置应用历史"""
    conn = get_conn()
    c = conn.cursor()
    c.execute('''SELECT * FROM application_logs WHERE device_id=?
                 ORDER BY created_at DESC LIMIT ?''', (device_id, limit))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows
