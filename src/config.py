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

# 运行环境：dev 本地存照片，prod 上传到飞书
APP_ENV = os.environ.get("APP_ENV", "dev").lower()

# ---- 登录配置（按 APP_ENV 切，不走环境变量）--------------------
# dev：密码登录（密码固定 "dev"）
# prod：飞书 OAuth（APP_ACCESS_PASSWORD 留空 → 密码登录整个走不通）
# APP_SECRET_KEY：cookie + OAuth state 的签名密钥
if APP_ENV == "prod":
    APP_ACCESS_PASSWORD = ""
    APP_SECRET_KEY = "Tna-4Y14PDJq1XYNl-7qycpjy1RG5EOVq8ll5NiJfL8"
else:
    APP_ACCESS_PASSWORD = "dev"
    APP_SECRET_KEY = "dev-only-not-for-prod"

# 本地照片目录（dev 模式）
PHOTO_DIR = PROJECT_ROOT / os.environ.get("PHOTO_DIR", "data/photos")
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

# Wiki root：固定 PROJECT_ROOT/wiki。
# Fly 上通过 start.sh 把 /app/wiki symlink 到 /data/wiki，代码层无感知。
WIKI_DIR = PROJECT_ROOT / "wiki"
WIKI_DIR.mkdir(parents=True, exist_ok=True)
(WIKI_DIR / "customers").mkdir(exist_ok=True)

# 危险运维接口的开关密码（如 /followup/{id}/regen-wiki）。
# 空串 → 接口 403 关闭。需要的话在 Fly secrets 里设 DEV_REGEN_PASSWORD。
DEV_REGEN_PASSWORD = os.environ.get("DEV_REGEN_PASSWORD", "")

# ByteHouse CRM
BH_HOST = os.environ.get("BH_HOST", "")
BH_PORT = int(os.environ.get("BH_PORT", "19000"))
BH_USER = os.environ.get("BH_USER", "")
BH_PASSWORD = os.environ.get("BH_PASSWORD", "")
BH_WAREHOUSE = os.environ.get("BH_WAREHOUSE", "")
BH_DATABASE_ODS = os.environ.get("BH_DATABASE_ODS", "ODS_YL")
BH_DATABASE_DWD = os.environ.get("BH_DATABASE_DWD", "DWD_YL")
