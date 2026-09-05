# 好眠 SnoozMate · 云端全栈框架 v2.0

> 对应完整时序图：五阶段全流程（登录绑定 → 睡前设置 → 夜间守护 → 晨间解读 → 用户确认）
> 技术栈：FastAPI + Pydantic；本地开发支持 SQLite，云托管生产使用 MySQL
> 微信云托管生产数据：使用同一环境的 MySQL，不能依赖容器文件
> 硬件：ESP32-S3 月石主机 + 枕下振动片（两件式）

---

## 快速启动

```bash
cd snoozmate-cloud-upload
pip install -r requirements.txt
python main.py
# 或
uvicorn main:app --port 8000
```

- API文档：http://localhost:8000/docs
- 管理仪表盘：http://localhost:8000/dashboard
- 根端点：http://localhost:8000/

## 生产部署：持久化、AI 与探针

**微信云托管当前不提供自定义持久卷挂载。** 容器会扩缩容、重启和换版本，SQLite 文件会随实例丢失。不要在微信云托管中把 `SNOOZMATE_PERSISTENT_VOLUME` 设为 `true` 来隐藏告警；这不会让磁盘变成持久存储。

结构化业务数据应使用控制台左侧的 **MySQL**。创建数据库后，在服务版本的环境变量中配置：

```bash
SNOOZMATE_DB_BACKEND=mysql
DB_HOST=内网地址
DB_PORT=3306
DB_USER=数据库用户
DB_PASSWORD=数据库密码
DB_NAME=数据库名
DB_CHARSET=utf8mb4
DB_POOL_SIZE=6
```

当前代码已支持 SQLite/MySQL 双后端：设置完整的 `DB_*` 变量（或显式 `SNOOZMATE_DB_BACKEND=mysql`）后会自动创建 MySQL 表、索引和幂等迁移；未配置 `DB_*` 时本地继续使用 SQLite。上线前可使用 `scripts/migrate_sqlite_to_mysql.py` 将旧 SQLite 数据导入 MySQL。迁移脚本会保留用户、设备、事件、摘要、反馈、候选、审计和 Bandit 数据，并在导入事件后修复旧摘要的 `event_count`。

部署后可在相同环境中运行 `python scripts/check_mysql.py`，它会验证连接、初始化表并输出数据库版本和表数量，不会输出密码。服务接口 `/readyz` 也会在 MySQL 无法连接时返回 503。

LLM 完全可选，默认使用确定性规则解读/对话，不会等待外部网络。要启用它，只在部署环境配置（不要放入源代码）：

```bash
SNOOZMATE_LLM_KEY=...
SNOOZMATE_LLM_BASE_URL=https://api.openai.com/v1
SNOOZMATE_LLM_MODEL=gpt-4o-mini
# 默认：普通 API connect 1.5s/read 3s/overall 4s；Ark Coding connect 2s/read 20s/overall 30s；可按需覆盖对应 *_SECONDS 变量
```

火山方舟等 OpenAI 兼容服务也可使用。普通 Ark API 使用 `/api/v3`；Coding Plan 的 `ark-code-latest` 使用 `/api/coding/v3`，并把 model 设为 `ark-code-latest`；密钥、base URL 和 model 三项必须来自同一个服务商。
服务也兼容常见变量 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` 以及 `ARK_API_KEY` / `ARK_MODEL` / `ARK_ENDPOINT_ID`。`GET /diagnostics` 中只有 `llm.configured=true` 才表示真实模型已启用；否则应用会明确显示“本地规则”，不会伪装成云端模型。
若使用火山方舟 Coding 兼容地址，可将 `SNOOZMATE_LLM_BASE_URL` 设为 `https://ark.cn-beijing.volces.com/api/coding/v3`，将 `SNOOZMATE_LLM_MODEL` 设为 `ark-code-latest`。为兼容已部署的旧 `/api/coding/v1` 变量，服务会自动规范化到 `/v3/chat/completions`，并使用受控的连接、读取和总超时。

晨报数据读取与 LLM 已解耦：`generate_ai=false` 只读结构化报告；`cached=true` 只使用已有解读或即时规则兜底，不会等待外部模型。`/ai/interpretation` 和 `/ai/chat` 才会尝试外部模型，并受总超时保护。

生产微信登录还需配置：

```bash
SNOOZMATE_WECHAT_APP_ID=...
SNOOZMATE_WECHAT_APP_SECRET=...
```

配置后服务端会调用微信 `jscode2session` 并使用稳定的 openid。未配置时仅进入调试模式，使用小程序本地生成并持久保存的 `client_id`，避免每个一次性登录 code 都创建新用户。`/healthz` 不访问数据库，适合 liveness；`/readyz` 验证当前配置的 SQLite 或 MySQL，适合 readiness；`/diagnostics` 会显示数据库后端、持久化、LLM 和微信登录模式，但不会返回任何密钥。声音和灯光 API 保存的是**期望状态**，不代表或模拟物理设备已经执行。

夜间事件按 `Asia/Shanghai` 的 **18:00 至次日 12:00** 归档；12:00-18:00 的白天调试事件仍可保留作审计，但 `night_id` 为空，不会进入昨夜报告、夜间事件列表或七晚趋势。可通过环境变量调整边界：

```bash
SNOOZMATE_NIGHT_TIMEZONE=Asia/Shanghai
SNOOZMATE_NIGHT_START_HOUR=18
SNOOZMATE_NIGHT_END_HOUR=12
```

## 真实设备数据与模拟数据

真实夜间数据只能由月石设备上报。设备无事件时，系统现在返回 `has_data=false`，不会再把“没有数据”解释为“昨夜很安静”。设备应：

1. 无事件期间定期调用 `POST /api/v1/device/{device_id}/heartbeat`；
2. 通过 `POST /api/v1/events/batch` 上传结构化事件；服务端会按时间戳逐条计算 `night_id`，不会接受客户端把白天事件强行归入某一晚；
3. 保留真实模型版本（如 `rule_v1`），不要使用 `simulator_*`。

需要联调七晚页面时，可显式生成**带模拟标记**的数据：

```bash
python scripts/seed_demo_data_cloud.py ^
  --base https://your-service.example.com ^
  --device device_esp32_real_001 ^
  --days 7 ^
  --confirm-simulated
```

模拟事件使用 `model_version=simulator_v1`，晨报、小程序趋势页和设备页会标明“模拟测试数据”，不会冒充真实睡眠记录。

## 测试

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试使用项目内隔离 SQLite 路径。`pytest.ini` 限制 pytest 只收集 `tests/`，避免 `scripts/` 中历史演示/压力脚本被意外当成测试执行。

小程序 Real 适配器、登录缓存、字段映射、超时、onboarding/setup 与手机音频生命周期测试：

```bash
cd ..\miniprogram\miniprogram
node tests\real-contract.test.js
node tests\page-smoke.test.js
```

## 小程序/公众号访问云托管

小程序如果只调用本环境的 `snoozmate-api`，推荐使用
`wx.cloud.callContainer`，而不是依赖 CloudBase 的默认公网域名。该方式走微信云调用链路，
不消耗公网流量，也不需要在小程序后台配置服务器域名；服务设置中的公网访问可以在验证完成后关闭。
小程序基础库需要至少 `2.23.0`，本项目配置为 `2.32.3`。

有效小程序配置位于 `..\miniprogram\miniprogram\config\env.js`：

```js
{
  transport: 'cloud-container',
  cloudEnvId: '你的 CloudBase 环境 ID',
  cloudServiceName: 'snoozmate-api'
}
```

`cloud-container` 强制微信客户端只使用 `wx.cloud.callContainer`，不会静默回退到即将过期的公网地址；
`public` 仅用于外部调试。Node 测试夹具会显式选择 `public`。
云调用请求路径仍是 `/api/v1/...`，并自动携带 `X-WX-SERVICE: snoozmate-api`。
`callContainer` 的单次 timeout 最大为 15 秒；Ark Coding 的复杂首次生成可能接近该上限，
应优先使用已缓存解读或短回复。

如果公众号与小程序不是同一主体/环境，需要按 CloudBase 资源复用或开放平台授权配置；
不能仅凭服务名直接跨主体调用。

跨栈测试需先启动本服务，再执行：

```bash
set SNOOZMATE_TEST_API_BASE=http://127.0.0.1:8000
node tests\real-http.integration.test.js
```

---

## 五阶段全流程 API 对照

### 阶段①：登录 + 设备绑定

| 步骤 | API | 说明 |
|---|---|---|
| 1.1 | `POST /api/v1/auth/login` | 用户登录（微信/测试码）→ 返回 session_token |
| 1.2 | `POST /api/v1/binding/token` | 用户申请绑定令牌（需Bearer鉴权） |
| 1.3 | `POST /api/v1/binding/confirm` | 设备端确认绑定（binding_token + device_id） |
| 1.4 | `GET /api/v1/user/devices` | 获取用户已绑定设备列表 |

```python
# 测试码登录（开发阶段）
POST /api/v1/auth/login  body: {"login_code": "test_code_xxx"}
# 返回：{"user_id": "...", "session_token": "sess_xxx", "nickname": "..."}
```

---

### 阶段②：睡前设置 / 配置同步

| 步骤 | API | 说明 |
|---|---|---|
| 2.1 | `GET /api/v1/device/{device_id}/config` | 设备拉取当前配置 + 版本号 |
| 2.2 | `POST /api/v1/agent/{device_id}/bedtime` | 通知"已上床" → Agent 进入入睡保护期 |
| 2.3 | `PUT /api/v1/device/{device_id}/config` | 用户/App 手动调整配置（有安全边界校验） |

配置项（DeviceConfig）：
```python
{
  "max_rounds_per_night": 6,         # 每夜最多干预轮数（预算上限）
  "cooldown_seconds": 600,            # 轮间冷却（10分钟）
  "fall_asleep_protection": 900,      # 入睡保护（上床后15分钟不干预）
  "max_vibration_duration_ms": 120000, # 单次最长振动（2分钟）
  "start_vibration_level": 1,         # 起始振动等级 1/2/3
  "max_vibration_level": 3,           # 允许升级到的最高等级 1-5
  "upgrade_interval_ms": 8000,        # 升级间隔 8 秒
  "snore_confirm_seconds": 10,        # 连续鼾声确认时间
  "snore_confidence_threshold": 0.65, # 鼾声置信度阈值
  "verify_window_seconds": 15,        # 干预后验证窗口
  "partner_mode_max_rounds": 4,       # 共享模式上限
}
```

---

### 阶段③：夜间守护（事件上报 + 实时决策）

| 步骤 | API | 说明 |
|---|---|---|
| 3.1 | `POST /api/v1/events` | 单条事件上报（设备 → 云端） |
| 3.2 | `POST /api/v1/events/batch` | 批量增量上报（夜间离线存本地，早上批量上传） |
| 3.3 | `POST /api/v1/agent/tick` | 实时 Agent 决策（调试/演示用，给当前状态返回下一步动作） |
| 3.4 | `GET /api/v1/events/{device_id}` | 事件列表（可按 night_id 过滤） |
| 3.5 | `GET /api/v1/events/{device_id}/night/{night_id}` | 单夜完整事件 + 摘要 |

**事件类型（event_type）**：
- `in_bed` / `out_of_bed` — 在床/离床
- `snore_start` / `snore_stop` — 鼾声开始/结束
- `vibration_start` / `vibration_stop` — 振动开始/结束
- `position_change` — 体位变化
- `intervention` — 干预事件（兼容旧命名）

**批量上报幂等**：同一 night_id 下，`UNIQUE(device_id, timestamp, event_type)` 自动去重。

**Agent 实时决策（`agent/tick`）** 输入输出：
```python
# 输入
{"device_id": "xxx", "audio_confidence": 0.85, "in_bed": true,
 "body_motion_level": 0.1, "temp_ok": true, "is_partner": false}
# 输出
{"action": "VIBRATE", "reason": "...", "level": 1, "state": "ACTIVE",
 "rounds_done": 2, "rounds_remaining": 4, "snore_streak": 12.5,
 "recent_log": [...]}
```

---

### 阶段④：晨间解读 + AI 建议 + 候选配置

| 步骤 | API | 说明 |
|---|---|---|
| 4.1 | `GET /api/v1/morning_report/{device_id}` | 完整晨报：数据 + AI解读 + 七晚趋势 |
| 4.2 | `GET /api/v1/weekly/{device_id}?days=7` | 周统计（成功率、趋势、参数变化） |
| 4.3 | `POST /api/v1/weekly/{device_id}/generate_candidate` | 生成 AI 建议的候选配置 |
| 4.4 | `GET /api/v1/candidates/{device_id}` | 候选配置列表（pending/applied/rejected） |
| 4.5 | `GET /api/v1/candidates/detail/{candidate_id}` | 候选详情 |

**晨报返回结构**：
```python
{
  "night_id": "night_20260901",
  "date": "20260901",
  "timeline": [...],          // 事件时间线
  "reminder_stats": {
    "total_count": 3,         // 总干预轮次
    "success_count": 2,       // 成功轮次
    "success_rate": 0.667,    // 成功率
    "avg_response_sec": 40.3, // 平均响应时间
    "max_level": 2,           // 最高振动等级
    "peak_hour": "2",         // 峰值时段
  },
  "weekly_trend": {...},      // 七晚趋势
  "ai_interpretation": {...}, // AI 解读（generate_ai=true时）
  "source_tag": "rule_based", // AI 来源
}
```

---

### 阶段⑤：用户确认 + 反馈 + 配置生效

| 步骤 | API | 说明 |
|---|---|---|
| 5.1 | `POST /api/v1/candidates/{candidate_id}/approve` | 用户批准候选 → 应用到设备（带幂等键） |
| 5.2 | `POST /api/v1/candidates/{candidate_id}/reject` | 用户拒绝候选 |
| 5.3 | `POST /api/v1/morning_feedback` | 晨间反馈（睡眠质量评分、主观感受） |
| 5.4 | `GET /api/v1/morning_feedback/{device_id}` | 历史反馈列表 |
| 5.5 | `GET /api/v1/application_logs/{device_id}` | 配置变更应用日志（审计追踪） |

**批准幂等**：同一个 `idempotency_key` 重复调用返回 `already_applied=true`，不会重复应用。

**应用日志**：每次配置变更都有完整审计（config_before、config_after、status、耗时、错误信息）。

---

## 数据库表结构

| 表 | 用途 |
|---|---|
| `users` | 用户（openid、session_token） |
| `device_bindings` | 用户-设备绑定关系 |
| `devices` | 设备元信息（配置版本、固件版本、最后上线） |
| `events` | 夜间事件流（主数据表，UNIQUE去重） |
| `daily_summaries` | 每日摘要（轮次、成功率、响应时间） |
| `config_candidates` | 候选配置（pending/applied/rejected） |
| `application_logs` | 配置应用审计日志 |
| `morning_feedback` | 晨间用户反馈 |

---

## 安全边界（硬编码，不可绕过）

| 参数 | 限值 | 说明 |
|---|---|---|
| max_rounds_per_night | ≤ 15 | 每晚最多15轮 |
| max_vibration_duration_ms | ≤ 300000 | 单次振动最长5分钟 |
| start_vibration_level | ≤ 3 | 起始等级不超3级 |
| cooldown_seconds | 60 ~ 1800 | 冷却 1 分钟 ~ 30 分钟 |

---

## 测试：五阶段全链路

```bash
# 所有 17 项 API 端点验证通过
# - 阶段1：登录/绑定/设备列表（4项）
# - 阶段2：配置获取/睡前通知（2项）
# - 阶段3：单条事件/批量事件/事件列表/Agent决策/夜详情（5项）
# - 阶段4：晨报/周统计/候选生成/候选列表（4项）
# - 阶段5：批准/幂等批准/反馈/应用日志/周报（5项）
# - 页面：仪表盘/API文档/根路径（3项）
```

---

## 目录结构

```
snoozmate-cloud/
├── main.py                  # FastAPI 入口
├── requirements.txt
├── README.md                # 本文档
├── app/
│   ├── config.py            # 配置（DB路径、默认参数、LLM）
│   ├── database.py          # 数据库 + 所有表的 CRUD
│   ├── models/schemas.py    # Pydantic 数据模型
│   ├── api/
│   │   ├── auth.py          # 登录 + 绑定
│   │   ├── events.py        # 事件上报（单条/批量）
│   │   └── agent.py         # Agent决策 + 晨报 + 候选 + 反馈
│   └── agent/
│       ├── engine.py        # Agent 状态机（实时决策）
│       ├── candidates.py    # 候选配置生成、批准、审计
│       └── ai_interpretation.py  # LLM 晨报解读（可选）
├── data/
│   └── snoozmate.db         # SQLite 数据库（自动创建）
├── scripts/
│   └── simulate_night.py    # 模拟一夜数据（测试用）
└── tests/                   # 测试目录
```

---

## 与硬件端的集成点

1. **设备端**（ESP32-S3 月石主机）：本地实时检测 + 干预，夜间断网照常工作
2. **云端**：早上设备联网后批量上传事件 → 云端生成摘要 + AI解读 + 候选配置 → 用户确认 → 设备下次同步拉取新配置
3. **配置版本号机制**：每次配置变更版本号+1，设备端对比版本号判断是否需要更新
4. **离线优先原则**：云端不参与实时干预，只做"解读 + 学习 + 建议"——永远不直接控制振动片
