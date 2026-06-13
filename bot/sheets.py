import httpx

from bot.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

_BASE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}",
    "Content-Type": "application/json",
}


async def append_return_row(
    date: str,
    time: str,
    employee: str,
    order_id: str,
    product: str,
    reason: str,
) -> None:
    payload = {
        "fields": {
            "Дата": date,
            "Время": time,
            "Сотрудник": employee,
            "ID заказа": order_id,
            "Товар": product,
            "Причина": reason,
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_BASE_URL, json=payload, headers=_HEADERS)
        resp.raise_for_status()
