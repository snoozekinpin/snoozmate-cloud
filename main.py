"""
SnoozMate 好眠 · 云端后端主入口（完整版）
5 阶段全流程：登录绑定 → 睡前设置 → 夜间守护 → 晨间解读 → 用户确认

启动：python main.py
仪表盘：http://localhost:8000/dashboard
API文档：http://localhost:8000/docs
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import logging

from app import config
from app.database import init_db, get_conn, database_diagnostics, upsert_device
from app.api import events, agent, auth

app = FastAPI(
    title="好眠 SnoozMate 云端 API",
    description="月石主机 + 枕下振动片 · 睡眠守护系统云端后端\n"
                "5 阶段：登录绑定 → 睡前设置 → 夜间守护 → 晨间解读 → 用户确认",
    version="2.0.0",
)
logger = logging.getLogger("snoozmate")
_database_ready = False
_database_error = ""
_database_error_code = None
_database_error_reason = ""

# CORS —— 前端联调必须（file://、localhost、小程序开发工具都会跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # 比赛演示环境，全放开
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    initialize_database()


def initialize_database():
    """Initialize storage, retaining a healthy HTTP process for diagnostics on failure."""
    global _database_ready, _database_error, _database_error_code, _database_error_reason
    _database_ready = False
    _database_error = ""
    _database_error_code = None
    _database_error_reason = ""
    try:
        init_db()
        if config.DEFAULT_DEVICE_ID:
            upsert_device(config.DEFAULT_DEVICE_ID)
        warning = database_diagnostics().get("durability_warning")
        if warning:
            logger.warning(warning)
        from app.seed_on_startup import seed_if_empty_with_log
        seed_if_empty_with_log()
    except Exception as error:
        code = error.args[0] if getattr(error, "args", ()) and isinstance(error.args[0], int) else None
        if code in {1044, 1045, 1142}:
            reason = "mysql_auth_or_permission"
        elif code == 1049:
            reason = "mysql_database_not_found"
        elif code in {2002, 2003, 2005}:
            reason = "mysql_network_unreachable"
        elif code in {1054, 1064, 1071, 1072, 1146}:
            reason = "mysql_schema_error"
        else:
            reason = type(error).__name__
        _database_ready = False
        _database_error = type(error).__name__
        _database_error_code = code
        _database_error_reason = reason
        logger.error("Database initialization failed (%s, code=%s). Check DB_* settings and network access.", reason, code)
        return False
    _database_ready = True
    _database_error = ""
    _database_error_code = None
    _database_error_reason = ""
    return True

# 注册路由
app.include_router(auth.router)
app.include_router(events.router)
app.include_router(agent.router)

# 静态文件（仪表盘）
static_dir = os.path.join(os.path.dirname(__file__), "app", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    return {
        "name": "好眠 SnoozMate 云端 API",
        "version": "2.0.0",
        "架构": "月石主机本地守护 + 云端AI解读 + 用户确认",
        "约束": "云端大模型不直接控制振动片，也不能绕过安全控制器",
        "API 分组": [
            "auth —— 登录 / 绑定 / 设备列表",
            "events —— 事件上报 / 增量同步",
            "agent —— 决策 / 配置 / 晨报 / 七晚 / 候选 / 反馈",
        ],
        "端点总览": [
            "POST /api/v1/auth/login",
            "POST /api/v1/binding/token",
            "POST /api/v1/binding/confirm",
            "GET  /api/v1/user/devices",
            "POST /api/v1/events",
            "POST /api/v1/events/batch",
            "GET  /api/v1/device/{id}/status",
            "GET  /api/v1/device/{id}/config",
            "PUT  /api/v1/device/{id}/config",
            "POST /api/v1/agent/tick",
            "GET  /api/v1/morning_report/{device_id}",
            "GET  /api/v1/weekly/{device_id}",
            "GET  /api/v1/candidates/{device_id}",
            "POST /api/v1/candidates/{id}/approve",
            "POST /api/v1/candidates/{id}/reject",
            "POST /api/v1/morning_feedback",
            "/dashboard (管理后台)",
            "/docs (Swagger API 文档)",
        ]
    }


@app.get("/healthz")
def liveness():
    """No database access: suitable for cheap container liveness probes."""
    return {"status": "ok"}


@app.get("/readyz")
def readiness():
    """Fail closed when the configured persistence backend cannot be queried."""
    if not _database_ready and not initialize_database():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail=f"database unavailable: {_database_error_reason or 'initialization_failed'}",
        )
    try:
        conn = get_conn()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return {"status": "ready"}
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="database unavailable")


@app.get("/diagnostics")
def diagnostics():
    """Operational metadata only; it intentionally exposes no credentials."""
    database = {
        **database_diagnostics(),
        "ready": _database_ready,
        "initialization_error": _database_error or None,
        "initialization_error_code": _database_error_code,
        "initialization_error_reason": _database_error_reason or None,
    }
    return {
        "status": "ok",
        "database": database,
        "llm": {
            "configured": bool(config.LLM_API_KEY),
            "provider": config.LLM_PROVIDER,
            "model": config.LLM_MODEL,
        },
        "wechat_login": {
            "mode": "code2session" if config.WECHAT_APP_ID and config.WECHAT_APP_SECRET else "debug-client-id",
        },
        "night_window": {
            "timezone": config.NIGHT_TIMEZONE,
            "start": f"{config.NIGHT_START_HOUR:02d}:00",
            "end": f"{config.NIGHT_END_HOUR:02d}:00",
            "daytime_events_excluded": True,
        },
    }

@app.get("/dashboard")
async def dashboard():
    path = os.path.join(static_dir, "dashboard.html")
    if os.path.exists(path):
        return FileResponse(path)
    return {"error": "dashboard not found"}


if __name__ == "__main__":
    import uvicorn
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(app, host="0.0.0.0", port=port)
