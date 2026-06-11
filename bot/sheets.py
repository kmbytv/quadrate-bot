import gspread
from google.oauth2.service_account import Credentials

from bot.config import GOOGLE_CREDENTIALS, SPREADSHEET_ID

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_HEADERS = ["Дата", "Время", "Сотрудник", "ID заказа", "Товар", "Причина"]


def _get_worksheet():
    creds = Credentials.from_service_account_info(GOOGLE_CREDENTIALS, scopes=_SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1

    # Ensure header row exists
    existing = ws.row_values(1)
    if existing != _HEADERS:
        ws.insert_row(_HEADERS, index=1)

    return ws


def append_return_row(date: str, time: str, employee: str, order_id: str, product: str, reason: str) -> int:
    ws = _get_worksheet()
    row = [date, time, employee, order_id, product, reason]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return ws.row_count
