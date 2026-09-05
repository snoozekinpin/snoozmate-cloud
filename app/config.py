"""Runtime configuration. Secrets are supplied only through environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _db_path() -> str:
    configured = os.environ.get("SNOOZMATE_DB_PATH")
    path = Path(configured).expanduser() if configured else BASE_DIR / "data" / "snoozmate.db"
    return str(path.resolve())


# Use an absolute path so the database never silently follows a process cwd.
DB_PATH = _db_path()
DB_BUSY_TIMEOUT_MS = int(os.environ.get("SNOOZMATE_DB_BUSY_TIMEOUT_MS", "5000"))
SEED_DEMO_DATA = _truthy("SNOOZMATE_SEED_DEMO_DATA")
DEFAULT_DEVICE_ID = os.environ.get("SNOOZMATE_DEFAULT_DEVICE_ID", "device_esp32_real_001").strip()
DEVICE_ONLINE_TTL_SECONDS = int(os.environ.get("SNOOZMATE_DEVICE_ONLINE_TTL_SECONDS", "300"))

# SQLite is the local-development default. CloudBase Run should provide all
# DB_* values so the service uses its managed MySQL instead of container files.
DB_HOST = os.environ.get("DB_HOST", "").strip()
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "").strip()
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "").strip()
DB_CHARSET = os.environ.get("DB_CHARSET", "utf8mb4").strip() or "utf8mb4"
DB_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "4"))
DB_READ_TIMEOUT_SECONDS = float(os.environ.get("DB_READ_TIMEOUT_SECONDS", "8"))
DB_WRITE_TIMEOUT_SECONDS = float(os.environ.get("DB_WRITE_TIMEOUT_SECONDS", "8"))
DB_POOL_SIZE = max(1, int(os.environ.get("DB_POOL_SIZE", "6")))
_requested_backend = os.environ.get("SNOOZMATE_DB_BACKEND", "").strip().lower()
if _requested_backend:
    DATABASE_BACKEND = _requested_backend
elif all((DB_HOST, DB_USER, DB_NAME)):
    DATABASE_BACKEND = "mysql"
else:
    DATABASE_BACKEND = "sqlite"
if DATABASE_BACKEND not in {"sqlite", "mysql"}:
    raise ValueError("SNOOZMATE_DB_BACKEND must be sqlite or mysql")

# A night is the evening window through the following late morning. Keeping
# the timezone and boundaries explicit prevents daytime diagnostics from being
# mistaken for the latest sleep record when a container uses UTC.
NIGHT_TIMEZONE = os.environ.get("SNOOZMATE_NIGHT_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai"
NIGHT_START_HOUR = int(os.environ.get("SNOOZMATE_NIGHT_START_HOUR", "18"))
NIGHT_END_HOUR = int(os.environ.get("SNOOZMATE_NIGHT_END_HOUR", "12"))
if not 0 <= NIGHT_START_HOUR <= 23 or not 0 <= NIGHT_END_HOUR <= 23 or NIGHT_START_HOUR <= NIGHT_END_HOUR:
    raise ValueError("SNOOZMATE_NIGHT_START_HOUR must be after NIGHT_END_HOUR within the same 24-hour cycle")

# LLM is optional. Credentials are read from deployment environment variables only.
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
_ARK_KEY = os.environ.get("ARK_API_KEY", "").strip()
LLM_API_KEY = os.environ.get("SNOOZMATE_LLM_KEY", "").strip() or _OPENAI_KEY or _ARK_KEY
_provider = os.environ.get("SNOOZMATE_LLM_PROVIDER", "").strip()
LLM_PROVIDER = _provider or ("volcengine-ark" if _ARK_KEY and not _OPENAI_KEY else "openai")
LLM_BASE_URL = os.environ.get("SNOOZMATE_LLM_BASE_URL", "").strip() or os.environ.get(
    "OPENAI_BASE_URL",
    "https://ark.cn-beijing.volces.com/api/coding/v3" if LLM_PROVIDER == "volcengine-ark" else "https://api.openai.com/v1",
).strip()
LLM_MODEL = (
    os.environ.get("SNOOZMATE_LLM_MODEL", "").strip()
    or os.environ.get("OPENAI_MODEL", "").strip()
    or os.environ.get("ARK_MODEL", "").strip()
    or os.environ.get("ARK_ENDPOINT_ID", "").strip()
    or ("ark-code-latest" if LLM_PROVIDER == "volcengine-ark" else "gpt-4o-mini")
)
_ark_llm = LLM_PROVIDER == "volcengine-ark" or "/api/coding/" in LLM_BASE_URL
if _ark_llm and LLM_BASE_URL.endswith("/api/coding/v1"):
    LLM_BASE_URL = LLM_BASE_URL[:-2] + "v3"
LLM_CONNECT_TIMEOUT_SECONDS = float(os.environ.get(
    "SNOOZMATE_LLM_CONNECT_TIMEOUT_SECONDS",
    "2" if _ark_llm else "1.5",
))
LLM_READ_TIMEOUT_SECONDS = float(os.environ.get(
    "SNOOZMATE_LLM_READ_TIMEOUT_SECONDS",
    "20" if _ark_llm else "3",
))
LLM_OVERALL_TIMEOUT_SECONDS = float(os.environ.get(
    "SNOOZMATE_LLM_OVERALL_TIMEOUT_SECONDS",
    "30" if _ark_llm else "4",
))

# test_code_* 登录只在显式开启时可用。生产必须保持关闭，否则任何人都能
# 用确定性的假 code 换到合法会话。
ALLOW_TEST_LOGIN = _truthy("SNOOZMATE_ALLOW_TEST_LOGIN")

# Configure these in production to exchange wx.login codes for a stable openid.
# During local/cloud debugging, the mini program sends a persistent installation ID
# so changing one-time login codes do not create a new user on every launch.
WECHAT_APP_ID = os.environ.get("SNOOZMATE_WECHAT_APP_ID", "").strip()
WECHAT_APP_SECRET = os.environ.get("SNOOZMATE_WECHAT_APP_SECRET", "").strip()
WECHAT_LOGIN_TIMEOUT_SECONDS = float(os.environ.get("SNOOZMATE_WECHAT_LOGIN_TIMEOUT_SECONDS", "3"))
SESSION_TTL_SECONDS = int(os.environ.get("SNOOZMATE_SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60)))

# Agent 默认参数（可被七晚学习覆盖）
DEFAULT_AGENT_CONFIG = {
    "max_rounds_per_night": 6,
    "cooldown_seconds": 600,
    "fall_asleep_protection": 900,
    "max_vibration_duration_ms": 120000,
    "start_vibration_level": 1,
    "max_vibration_level": 3,
    "upgrade_interval_ms": 8000,
    "snore_confirm_seconds": 10,
    "snore_confidence_threshold": 0.65,
    "verify_window_seconds": 15,
    "partner_mode_max_rounds": 4,
}

AUDIO_CONFIG = {
    "sample_rate": 16000,
    "n_mfcc": 13,
    "n_fft": 512,
    "hop_length": 256,
    "frame_seconds": 1.0,
}
