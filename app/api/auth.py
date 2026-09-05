"""
用户 & 绑定 API
对应时序图第 ① 阶段
"""
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import hashlib
import httpx

from app import config
from app.database import (
    gen_id,
    get_conn,
    get_user_by_token,
    get_user_profile,
    update_user_profile,
    upsert_device,
    upsert_user_session,
)

router = APIRouter(prefix="/api/v1", tags=["user & binding"])


# ═══════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════

class LoginRequest(BaseModel):
    login_code: str = Field(min_length=1, max_length=256)
    client_id: str = Field(default="", max_length=128)


class BindRequest(BaseModel):
    binding_token: str
    device_id: str


# ═══════════════════════════════════════
# 登录（微信登录简化版）
# ═══════════════════════════════════════

@router.post("/auth/login")
async def login(req: LoginRequest):
    """
    微信登录 → 返回 userId + 业务会话
    开发阶段可用假 code 测试（test_code_xxx → user_xxx），需显式设置
    SNOOZMATE_ALLOW_TEST_LOGIN=true；生产环境保持关闭。
    """
    now = int(datetime.now().timestamp())
    user_id, openid = await _resolve_login_identity(req)
    session_token = gen_id("sess")
    expires_at = now + config.SESSION_TTL_SECONDS
    user = upsert_user_session(user_id, openid, session_token, expires_at)
    profile = {
        "id": user_id,
        "nickname": user.get("nickname") or "月石用户",
        "sleepMode": user.get("sleep_mode") or "shared",
        "privacyAccepted": bool(user.get("privacy_accepted")),
        "aiDataAuthorized": bool(user.get("ai_data_authorized")),
        "aiConsentVersion": user.get("ai_consent_version") or "",
    }
    return {
        "user_id": user_id,
        "session_token": session_token,
        "nickname": user.get("nickname") or "月石用户",
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat(),
        "expires_in": config.SESSION_TTL_SECONDS,
        "profile": profile,
    }


# ═══════════════════════════════════════
# 设备绑定令牌
# ═══════════════════════════════════════

@router.post("/binding/token")
def get_binding_token(
    device_id: str = "",
    authorization: str = Header(default=""),
):
    """
    申请设备绑定令牌（一次性）
    用户要绑定设备前先申请 token
    """
    user_id = _get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="未登录")

    # 确保设备存在
    upsert_device(device_id)

    conn = get_conn()
    c = conn.cursor()
    binding_token = gen_id("bind")
    now = int(datetime.now().timestamp())

    c.execute('''INSERT INTO device_bindings (user_id, device_id, binding_token, status, created_at)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(user_id,device_id) DO UPDATE SET binding_token=excluded.binding_token,
                 status='pending', created_at=excluded.created_at''',
              (user_id, device_id, binding_token, "pending", now))
    conn.commit()
    conn.close()

    return {
        "binding_token": binding_token,
        "device_id": device_id,
        "expires_in": 3600,
    }


@router.post("/binding/confirm")
def confirm_binding(req: BindRequest):
    """
    设备端确认绑定（设备拿到 binding_token 后调用）
    把状态从 pending 改成 active
    """
    conn = get_conn()
    c = conn.cursor()
    now = int(datetime.now().timestamp())

    c.execute(
        """SELECT * FROM device_bindings WHERE binding_token=? AND device_id=?
           AND status='pending' AND created_at>?""",
        (req.binding_token, req.device_id, now - 3600),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="绑定令牌无效")

    c.execute('''UPDATE device_bindings SET status='active' WHERE id=?''', (row["id"],))
    # 更新设备上线时间
    c.execute('''UPDATE devices SET last_online=? WHERE device_id=?''', (now, req.device_id))
    conn.commit()

    # Return data already known in this transaction; avoid opening a second DB
    # connection while this write transaction is still active.
    device = c.execute("SELECT config_version FROM devices WHERE device_id=?", (req.device_id,)).fetchone()
    conn.close()

    return {
        "status": "active",
        "device_id": req.device_id,
        "config_version": device["config_version"] if device else 1,
    }


@router.get("/user/devices")
def list_user_devices(authorization: str = Header(default="")):
    """获取用户的设备列表 - 允许无 token 返回 demo 设备"""
    user_id = _get_user_from_token(authorization)
    # 无 token 时返回 demo 设备（不报错）
    if not user_id:
        return {
            "user_id": "demo-user",
            "devices": [_get_demo_device()],
            "count": 1,
        }

    conn = get_conn()
    c = conn.cursor()
    c.execute('''SELECT d.*, b.role FROM device_bindings b
                 JOIN devices d ON b.device_id = d.device_id
                 WHERE b.user_id=? AND b.status='active' ''', (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"user_id": user_id, "devices": rows}



@router.get("/user/me/profile")
def get_me_profile(authorization: str = Header(default="")):
    """获取持久化 profile（兼容前端字段）"""
    user_id = _get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="AUTH_EXPIRED")
    return get_user_profile(user_id)


@router.put("/user/me/profile")
def save_me_profile(authorization: str = Header(default=""), body: dict = None):
    """Patch and persist profile without resetting omitted consent fields."""
    user_id = _get_user_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="AUTH_EXPIRED")
    incoming = body or {}
    aliases = {
        "nickname": "nickname",
        "sleepMode": "sleepMode",
        "sleep_mode": "sleepMode",
        "privacyAccepted": "privacyAccepted",
        "privacy_accepted": "privacyAccepted",
        "aiDataAuthorized": "aiDataAuthorized",
        "ai_data_authorized": "aiDataAuthorized",
        "aiConsentVersion": "aiConsentVersion",
        "ai_consent_version": "aiConsentVersion",
    }
    patch = {target: incoming[source] for source, target in aliases.items() if source in incoming}
    if "nickname" in patch:
        patch["nickname"] = str(patch["nickname"]).strip()
        if not patch["nickname"] or len(patch["nickname"]) > 40:
            raise HTTPException(status_code=400, detail="nickname 长度必须为 1-40")
    if "sleepMode" in patch and patch["sleepMode"] not in {"solo", "shared"}:
        raise HTTPException(status_code=400, detail="sleepMode 必须为 solo 或 shared")
    return update_user_profile(user_id, patch)


# ═══════════════════════════════════════
# 工具
# ═══════════════════════════════════════

def _get_user_from_token(auth_header: str) -> str:
    """从 Authorization header 提取用户ID
    简化版：Bearer <session_token>
    """
    if not auth_header or not auth_header.startswith("Bearer "):
        return ""
    token = auth_header.replace("Bearer ", "").strip()
    user = get_user_by_token(token)
    return user.get("user_id", "")


async def _resolve_login_identity(req: LoginRequest) -> tuple[str, str]:
    if not req.login_code.strip():
        raise HTTPException(status_code=400, detail="login_code 不能为空")
    if req.login_code.startswith("test_code_"):
        # 未开启开关时不承认测试码，且复用与凭据无效相同的响应，避免对外
        # 暴露存在这条测试通道。
        if not config.ALLOW_TEST_LOGIN:
            raise HTTPException(status_code=401, detail="微信登录凭据无效")
        suffix = req.login_code.removeprefix("test_code_") or "default"
        return f"user_{suffix}", f"test:{suffix}"

    if config.WECHAT_APP_ID and config.WECHAT_APP_SECRET:
        timeout = httpx.Timeout(config.WECHAT_LOGIN_TIMEOUT_SECONDS)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.get(
                    "https://api.weixin.qq.com/sns/jscode2session",
                    params={
                        "appid": config.WECHAT_APP_ID,
                        "secret": config.WECHAT_APP_SECRET,
                        "js_code": req.login_code,
                        "grant_type": "authorization_code",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="微信登录服务暂时不可用") from exc
        openid = str(payload.get("openid") or "").strip()
        if not openid:
            raise HTTPException(status_code=401, detail="微信登录凭据无效")
        digest = hashlib.sha256(openid.encode()).hexdigest()[:20]
        return f"wx_{digest}", f"wx:{digest}"

    # Debug fallback: client_id is generated once by the mini program and remains
    # stable even though wx.login returns a different one-time code on each call.
    identity = req.client_id.strip() or req.login_code.strip()
    namespace = "client" if req.client_id.strip() else "code"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return f"{namespace}_{digest}", f"{namespace}:{digest}"


def _get_demo_device() -> dict:
    """返回演示设备（无 token 时使用）"""
    return {
        "device_id": "device_esp32_real_001",
        "name": "月石床头主机",
        "model": "SnoozMate-v1",
        "firmware_version": "v1.0.3",
        "status": "online",
        "bound_at": "2026-08-25T10:00:00Z",
        "last_seen": "2026-09-03T17:30:00Z",
    }
