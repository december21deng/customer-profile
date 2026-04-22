import os
from pathlib import Path
from dotenv import load_dotenv

# Ensure .env is loaded from project root regardless of cwd
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_env_path = PROJECT_ROOT / ".env"
# override=False：shell 里已经 export 的变量优先于 .env（便于临时测试、CI）
load_dotenv(_env_path, override=False)

LARK_APP_ID = os.environ["LARK_APP_ID"]
LARK_APP_SECRET = os.environ["LARK_APP_SECRET"]
LARK_VERIFICATION_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "")
LARK_ENCRYPT_KEY = os.environ.get("LARK_ENCRYPT_KEY", "")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AGENT_ID = os.environ.get("AGENT_ID", "")
AGENT_VERSION = os.environ.get("AGENT_VERSION", "")

# v0.1 的 session store（bot thread 映射）
DB_PATH = os.environ.get("DB_PATH", "sessions.db")
INGEST_INTERVAL_SECONDS = int(os.environ.get("INGEST_INTERVAL_SECONDS", "3600"))

# v0.2 应用数据库（customers / followup_records / wiki 索引 / CRM 镜像）
APP_DB_PATH = PROJECT_ROOT / os.environ.get("APP_DB_PATH", "db/app.sqlite")
APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Wiki root directory (where Claude reads/writes customer knowledge)
WIKI_DIR = PROJECT_ROOT / "wiki"
WIKI_DIR.mkdir(exist_ok=True)
(WIKI_DIR / "customers").mkdir(exist_ok=True)
(WIKI_DIR / "contacts").mkdir(exist_ok=True)

# ByteHouse CRM
BH_HOST = os.environ.get("BH_HOST", "")
BH_PORT = int(os.environ.get("BH_PORT", "19000"))
BH_USER = os.environ.get("BH_USER", "")
BH_PASSWORD = os.environ.get("BH_PASSWORD", "")
BH_WAREHOUSE = os.environ.get("BH_WAREHOUSE", "")
BH_DATABASE_ODS = os.environ.get("BH_DATABASE_ODS", "ODS_YL")
BH_DATABASE_DWD = os.environ.get("BH_DATABASE_DWD", "DWD_YL")
