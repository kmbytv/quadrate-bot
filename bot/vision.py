import base64
import json
import re
import httpx

from bot.config import OPENAI_API_KEY

_SYSTEM_PROMPT = """Ты — ассистент, который извлекает данные из скриншотов возвратов приложения Озон.
Верни ТОЛЬКО валидный JSON без markdown-блоков и без пояснений.
Формат ответа:
{
  "order_id": "<число после # в заголовке, только цифры>",
  "product": "<название товара>",
  "reason": "<причина возврата или аннуляции>",
  "time": "<время из статусбара телефона, формат ЧЧ:ММ>"
}
Если поле не удаётся распознать — подставь значение "—"."""

_API_URL = "https://api.openai.com/v1/chat/completions"


async def extract_return_data(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
                    },
                    {"type": "text", "text": "Извлеки данные из этого скриншота возврата Озон."},
                ],
            },
        ],
        "max_tokens": 300,
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(_API_URL, json=payload, headers=headers)
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences if model wraps response anyway
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.DOTALL).strip()

    data = json.loads(content)
    for key in ("order_id", "product", "reason", "time"):
        if not data.get(key):
            data[key] = "—"
    return data
