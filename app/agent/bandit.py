# -*- coding: utf-8 -*-
"""
Bandit 在线学习引擎 —— Agent 自主控制核心
=================================================
实现 Thompson Sampling 多参数联合优化：
  - 3个可调参数：start_vibration_level / cooldown_seconds / snore_confidence_threshold
  - 每晚结束（晨报反馈）→ 更新 Beta 后验
  - 生成候选配置时从后验采样 → "探索 vs 利用"平衡

设计原则（比赛叙事友好）：
  - 完全可解释：每个参数的 alpha/beta 都能讲出故事
  - 安全兜底：采样结果永远被 SAFETY_LIMITS 裁剪
  - 数据留存：每次更新写入 DB，演示"学习曲线"
"""
import json
import math
import random
from datetime import datetime
from typing import Dict, List, Optional

from app.database import get_conn, get_device_config, gen_id
from app.config import DATABASE_BACKEND

# ─── 安全边界（和 candidates.py / config 校验一致） ───
SAFETY_LIMITS = {
    "start_vibration_level": (1, 3),        # L1~L3
    "cooldown_seconds": (60, 1800),          # 1~30分钟
    "snore_confidence_threshold": (0.4, 0.9), # 置信度门限
    "snore_confirm_seconds": (3, 30),         # 确认时长
}

# 每个参数的离散选项空间（Thompson采样在离散臂上做）
PARAM_ARMS = {
    "start_vibration_level": [1, 2, 3],
    "cooldown_seconds": [300, 600, 900, 1200],
    "snore_confidence_threshold": [0.55, 0.65, 0.75],
    "snore_confirm_seconds": [5, 10, 15],
}


class BanditEngine:
    """每个设备一个 Bandit 实例（参数存 SQLite）"""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._ensure_table()
        self._ensure_state()

    # ─── 存储 ───
    def _ensure_table(self):
        conn = get_conn()
        c = conn.cursor()
        if DATABASE_BACKEND == "mysql":
            c.execute("""CREATE TABLE IF NOT EXISTS bandit_state (
                device_id VARCHAR(128) PRIMARY KEY,
                state_json LONGTEXT NOT NULL,
                nights_tracked INT DEFAULT 0,
                updated_at BIGINT DEFAULT 0
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            c.execute("""CREATE TABLE IF NOT EXISTS bandit_history (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                device_id VARCHAR(128) NOT NULL,
                night_id VARCHAR(256) NOT NULL,
                param_name VARCHAR(128) NOT NULL,
                param_value DOUBLE NOT NULL,
                reward DOUBLE NOT NULL,
                alpha_before DOUBLE, beta_before DOUBLE,
                alpha_after DOUBLE, beta_after DOUBLE,
                created_at BIGINT DEFAULT 0,
                KEY idx_bandit_history_device (device_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        else:
            c.execute('''CREATE TABLE IF NOT EXISTS bandit_state (
                device_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                nights_tracked INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT 0
            )''')
            # 学习历史（给演示画曲线用）
            c.execute('''CREATE TABLE IF NOT EXISTS bandit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                night_id TEXT NOT NULL,
                param_name TEXT NOT NULL,
                param_value REAL NOT NULL,
                reward REAL NOT NULL,
                alpha_before REAL, beta_before REAL,
                alpha_after REAL, beta_after REAL,
                created_at INTEGER DEFAULT 0
            )''')
        conn.commit()
        conn.close()

    def _ensure_state(self):
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT state_json FROM bandit_state WHERE device_id=?", (self.device_id,))
        row = c.fetchone()
        if row:
            self.state = json.loads(row["state_json"])
        else:
            # 初始化：每个参数的每个臂 = Beta(1,1) 均匀先验
            self.state = {
                param: {str(arm): {"alpha": 1.0, "beta": 1.0} for arm in arms}
                for param, arms in PARAM_ARMS.items()
            }
            self._save()
        conn.close()

    def _save(self):
        conn = get_conn()
        c = conn.cursor()
        now = int(datetime.now().timestamp())
        c.execute('''INSERT INTO bandit_state (device_id, state_json, nights_tracked, updated_at)
                     VALUES (?,?,?,?)
                     ON CONFLICT(device_id) DO UPDATE SET
                       state_json=excluded.state_json,
                       nights_tracked=excluded.nights_tracked,
                       updated_at=excluded.updated_at''',
                  (self.device_id, json.dumps(self.state), self.state.get("_nights", 0), now))
        conn.commit()
        conn.close()

    # ─── 核心逻辑 ───
    def sample_config(self) -> Dict:
        """Thompson 采样：从每个参数的 Beta 后验采样，选出最优臂"""
        chosen = {}
        for param in PARAM_ARMS:
            samples = {}
            for arm_key, stats in self.state[param].items():
                a, b = stats["alpha"], stats["beta"]
                # Beta 分布采样（标准库，无 numpy 依赖）
                samples[float(arm_key)] = random.betavariate(a, b)
            if samples:
                chosen[param] = max(samples, key=samples.get)
        # 裁剪到安全范围
        for k, (lo, hi) in SAFETY_LIMITS.items():
            if k in chosen:
                chosen[k] = max(lo, min(hi, chosen[k]))
        return chosen

    def update_from_night(self, night_id: str, reward: float, config_used: Dict):
        """
        一晚结束 → 更新 Beta 后验
        reward: 0~1（综合晨报反馈 + 成功率计算）
        config_used: 当晚实际使用的参数值
        """
        for param, value in config_used.items():
            if param not in PARAM_ARMS:
                continue
            arm_key = str(self._nearest_arm(param, value))
            stats = self.state[param][arm_key]
            a_before, b_before = stats["alpha"], stats["beta"]
            # Bernoulli 化：reward>=0.5 记成功
            if reward >= 0.5:
                stats["alpha"] += 1
            else:
                stats["beta"] += 1
            self._log_history(night_id, param, value, reward,
                              a_before, b_before, stats["alpha"], stats["beta"])
        self.state["_nights"] = self.state.get("_nights", 0) + 1
        self._save()

    def _nearest_arm(self, param, value):
        arms = PARAM_ARMS[param]
        return min(arms, key=lambda a: abs(a - value))

    def _log_history(self, night_id, param, value, reward, ab, bb, aa, ba):
        conn = get_conn()
        c = conn.cursor()
        now = int(datetime.now().timestamp())
        c.execute('''INSERT INTO bandit_history
                     (device_id, night_id, param_name, param_value, reward,
                      alpha_before, beta_before, alpha_after, beta_after, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?)''',
                  (self.device_id, night_id, param, value, reward, ab, bb, aa, ba, now))
        conn.commit()
        conn.close()

    # ─── 展示用 ───
    def get_learning_curve(self) -> List[dict]:
        """学习曲线（演示用）：每晚平均 reward 趋势"""
        conn = get_conn()
        c = conn.cursor()
        c.execute('''SELECT night_id, AVG(reward) as avg_reward, COUNT(*) as n,
                            MIN(created_at) as first_created_at
                     FROM bandit_history WHERE device_id=?
                     GROUP BY night_id ORDER BY first_created_at''', (self.device_id,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_posterior_summary(self) -> Dict:
        """当前后验分布（讲解用）：每个参数每个臂的成功概率估计"""
        result = {}
        for param, arms in self.state.items():
            if param.startswith("_"):
                continue
            result[param] = {
                arm_key: {
                    "expected_prob": round(s["alpha"] / (s["alpha"] + s["beta"]), 3),
                    "alpha": s["alpha"], "beta": s["beta"],
                    "confidence": round((s["alpha"] + s["beta"]) / (s["alpha"] + s["beta"] + 10), 3),
                }
                for arm_key, s in arms.items()
            }
        return result

    def reset(self):
        self.state = {
            param: {str(arm): {"alpha": 1.0, "beta": 1.0} for arm in arms}
            for param, arms in PARAM_ARMS.items()
        }
        self._save()


# ─── Reward 计算 ───
def compute_night_reward(success_rate: float, avg_response_time: float,
                          was_disturbed: bool, morning_feeling: int,
                          rounds: int) -> float:
    """
    一晚的 reward（0~1）：
    - 干预成功率 50%
    - 晨间感受 30%（5分制→0~1）
    - 打扰惩罚 20%（被吵醒 = 大扣分）
    """
    sr = max(0, min(1, success_rate))
    feeling = max(0, min(1, (morning_feeling - 1) / 4))
    disturb_pen = 0 if was_disturbed else 1
    r = 0.5 * sr + 0.3 * feeling + 0.2 * disturb_pen
    # 轻度正则：轮数适中更好（防爆轮）
    if rounds > 8:
        r *= 0.8
    return round(max(0, min(1, r)), 3)


# ─── 与现有候选流集成 ───
def generate_bandit_candidate(device_id: str, night_id: str = "") -> str:
    """
    从 Bandit 后验采样生成候选配置（替代/增强 generate_ai_candidate）
    返回 candidate_id
    """
    bandit = BanditEngine(device_id)
    sampled = bandit.sample_config()
    current = get_device_config(device_id)

    # 采样值与当前配置的差异 = 学习信号
    changes = {k: v for k, v in sampled.items() if current.get(k) != v}
    summary = "Bandit学习采样：" + (", ".join(f"{k}={v}" for k, v in changes.items()) if changes else "保持当前（后验已收敛）")

    from app.agent.candidates import create_candidate
    return create_candidate(
        device_id=device_id,
        suggested_config={**current, **sampled},
        source="bandit",
        summary=summary,
        basis=[f"Thompson采样于 {bandit.state.get('_nights', 0)} 晚数据",
               "Beta后验" + json.dumps({k: f"{v:.2f}" for k, v in _posterior_means(bandit).items()})],
    )


def _posterior_means(bandit: BanditEngine) -> Dict[str, float]:
    """每个参数当前后验的期望值（用于 basis 展示）"""
    means = {}
    for param, arms in bandit.state.items():
        if param.startswith("_"):
            continue
        total_a = sum(s["alpha"] for s in arms.values())
        total_b = sum(s["beta"] for s in arms.values())
        means[param] = total_a / (total_a + total_b + 1e-9)
    return means


def update_bandit_after_night(device_id: str, night_id: str,
                               success_rate: float, avg_response_time: float,
                               was_disturbed: bool, morning_feeling: int,
                               rounds: int, config_used: Optional[Dict] = None):
    """晨报反馈后调用：更新后验"""
    if config_used is None:
        config_used = get_device_config(device_id)
    reward = compute_night_reward(success_rate, avg_response_time,
                                   was_disturbed, morning_feeling, rounds)
    bandit = BanditEngine(device_id)
    bandit.update_from_night(night_id, reward, config_used)
    return {"reward": reward, "nights": bandit.state.get("_nights", 0)}
