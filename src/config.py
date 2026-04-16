import os
from dotenv import load_dotenv

load_dotenv()

LARK_APP_ID = os.environ["LARK_APP_ID"]
LARK_APP_SECRET = os.environ["LARK_APP_SECRET"]
LARK_VERIFICATION_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "")
LARK_ENCRYPT_KEY = os.environ.get("LARK_ENCRYPT_KEY", "")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AGENT_ID = os.environ["AGENT_ID"]
AGENT_VERSION = os.environ.get("AGENT_VERSION", "")

DB_PATH = os.environ.get("DB_PATH", "sessions.db")
INGEST_INTERVAL_SECONDS = int(os.environ.get("INGEST_INTERVAL_SECONDS", "3600"))
