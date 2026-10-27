"""全域設定模組

集中管理所有環境變數 (Aria 憑證、Gemini、Ollama、SSH Key、Tickets 路徑) 與 prompts.toml 載入。
"""
import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 專案根目錄
BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    return p if p.is_absolute() else (BASE_DIR / p)


# ─── Gemini (Supervisor 與 VM Provisioning 子圖使用) ───
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# ─── VMware Aria Automation ───
AUTOMATION_URL: str = os.getenv("AUTOMATION_URL", "").rstrip("/")
AUTOMATION_API_TOKEN: str = os.getenv("AUTOMATION_API_TOKEN", "")
AUTOMATION_VERIFY_SSL: bool = _env_bool("AUTOMATION_VERIFY_SSL", False)
AUTOMATION_HTTP_TIMEOUT: int = _env_int("AUTOMATION_HTTP_TIMEOUT", 15)

# ─── 本地 Ollama (工單解析) ───
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma3:1b")
OLLAMA_TIMEOUT: int = _env_int("OLLAMA_TIMEOUT", 300)
# 工單原文 + 系統提示可能超過 Ollama 預設 context (2048/4096)，需放大避免被截斷
OLLAMA_NUM_CTX: int = _env_int("OLLAMA_NUM_CTX", 8192)
OLLAMA_MAX_RAW_CHARS: int = _env_int("OLLAMA_MAX_RAW_CHARS", 6000)

# ─── SSH (VM 診斷) ───
SSH_PRIVATE_KEY_PATH: Path = _env_path("SSH_PRIVATE_KEY_PATH", Path.home() / ".ssh" / "id_rsa")
SSH_KEY_PASSPHRASE: str | None = os.getenv("SSH_KEY_PASSPHRASE") or None
DEFAULT_SSH_USER: str = os.getenv("DEFAULT_SSH_USER", "root")
SSH_PORT: int = _env_int("SSH_PORT", 22)
SSH_TIMEOUT: int = _env_int("SSH_TIMEOUT", 10)
# true: 只連線 known_hosts 中已存在的主機；false: 自動接受新主機金鑰 (內網維運常見設定)
SSH_STRICT_HOST_KEY: bool = _env_bool("SSH_STRICT_HOST_KEY", False)

# ─── 工單資料夾 ───
TICKETS_DIR: Path = _env_path("TICKETS_DIR", BASE_DIR / "tickets")
TICKET_EXTENSIONS: tuple[str, ...] = (".txt", ".md")

# ─── Prompts ───
def _load_prompts() -> dict[str, Any]:
    if not PROMPTS_PATH.exists():
        return {}
    with open(PROMPTS_PATH, "rb") as f:
        return tomllib.load(f)

PROMPTS_PATH: Path = BASE_DIR / "prompts.toml"
PROMPTS: dict[str, Any] = _load_prompts()
