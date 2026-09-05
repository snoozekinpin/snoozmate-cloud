# -*- coding: utf-8 -*-
"""
SnoozMate 演示数据自动播种
Only enabled explicitly for local demos. Production startup must never insert demo
records just because a persisted database is empty.
"""
import random
import time
from datetime import datetime, timedelta

from app.database import (
    get_conn, init_db, upsert_device, insert_event,
    get_weekly_stats, get_recent_events,
)
from app.config import SEED_DEMO_DATA

DEV = "device_esp32_real_001"
SEED_VERSION = "v3"  # 改动播种逻辑时递增，触发重新播种


def _seed_if_empty():
    """数据库没有任何事件 → 灌 7 晚演示数据"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM events")
    n = c.fetchone()[0]
    conn.close()
    return n == 0


def seed_demo_data():
    """灌 7 晚演示数据 + bandit 学习曲线。幂等：只在空库时执行。"""
    init_db()
    if not _seed_if_empty():
        return {"seeded": False, "reason": "db not empty"}

    random.seed(42)
    upsert_device(DEV, name="月石主机", mode="solo", last_online=int(time.time()))

    # Generate completed nights only; never create future 23:00 events.
    today = datetime.now() - timedelta(days=1)
    total_events = 0
    for i in range(6, -1, -1):
        night_date = today - timedelta(days=i)
        rounds = random.choice([3, 4, 4, 5])
        success_rate = 0.4 + (6 - i) * 0.08  # 40% → 88%
        start = datetime(night_date.year, night_date.month, night_date.day, 23, random.choice([5, 18, 40]))
        base_ts = int(start.timestamp())

        events = [
            {"timestamp": base_ts, "event_type": "in_bed", "in_bed": True, "body_motion_level": 0.1},
            {"timestamp": base_ts + 5, "event_type": "body_motion", "body_motion_level": 0.8, "in_bed": True},
        ]

        ts = base_ts + 1800
        for rn in range(1, rounds + 1):
            level = random.choice([1, 1, 2, 2, 3]) if (6 - i) >= 3 else random.choice([1, 1, 2])
            is_success = random.random() < success_rate
            events.append({
                "timestamp": ts, "event_type": "snore_detected",
                "snore_confidence": round(random.uniform(0.6, 0.9), 2),
                "snore_duration_sec": round(random.uniform(5, 25), 1),
                "round_in_night": rn,
            })
            events.append({
                "timestamp": ts + 10, "event_type": "intervention",
                "vibration_level": level, "vibration_duration_ms": level * 4000,
                "round_in_night": rn, "result": "",
            })
            if is_success:
                rt = round(random.uniform(8, 30), 1)
                events.append({
                    "timestamp": ts + 10 + int(rt), "event_type": "position_change",
                    "round_in_night": rn, "result": "success",
                    "response_time_sec": rt,
                })
            ts += random.randint(1200, 2700)

        events.append({"timestamp": ts + 3600, "event_type": "wake_up", "in_bed": False})

        for e in events:
            insert_event({
                "device_id": DEV,
                "model_version": "simulator_v1",
                "note": "simulated-demo-data",
                **e,
            })
            total_events += 1

    # 重算 7 晚 summary（晨报/趋势的数据源）
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT DISTINCT night_id FROM events WHERE device_id=?", (DEV,))
    night_ids = [r["night_id"] for r in c.fetchall()]
    conn.close()

    from app.database import compute_daily_summary
    for nid in night_ids:
        compute_daily_summary(DEV, nid)

    return {"seeded": True, "events": total_events, "nights": len(night_ids)}


def seed_if_empty_with_log():
    """Startup entry point. Seeding is deliberately opt-in."""
    if not SEED_DEMO_DATA:
        return {"seeded": False, "reason": "SNOOZMATE_SEED_DEMO_DATA is not enabled"}
    try:
        t0 = time.time()
        result = seed_demo_data()
        dt = time.time() - t0
        if result.get("seeded"):
            print(f"[seed] ✅ 自动灌入演示数据: {result['events']} 事件 / {result['nights']} 晚 / {dt:.1f}s")
        else:
            print(f"[seed] 跳过: {result.get('reason')}")
        return result
    except Exception as e:
        print(f"[seed] ❌ 播种失败（不影响启动）: {e}")
        return {"seeded": False, "error": str(e)}
