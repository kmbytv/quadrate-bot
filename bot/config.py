import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
DIRECTOR_CHAT_ID: int = int(os.environ["DIRECTOR_CHAT_ID"])

_raw_whitelist = os.environ.get("WHITELIST_IDS", "")
WHITELIST: set[int] = {int(x.strip()) for x in _raw_whitelist.split(",") if x.strip()}

# Airtable
AIRTABLE_API_KEY: str = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID: str = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_TABLE_NAME: str = os.environ.get("AIRTABLE_TABLE_NAME", "Возвраты")
