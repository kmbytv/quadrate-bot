import os
import json
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
SPREADSHEET_ID: str = os.environ["SPREADSHEET_ID"]
DIRECTOR_CHAT_ID: int = int(os.environ["DIRECTOR_CHAT_ID"])

_raw_whitelist = os.environ.get("WHITELIST_IDS", "")
WHITELIST: set[int] = {int(x.strip()) for x in _raw_whitelist.split(",") if x.strip()}

_google_creds_raw: str = os.environ["GOOGLE_CREDENTIALS_JSON"]
GOOGLE_CREDENTIALS: dict = json.loads(_google_creds_raw)
