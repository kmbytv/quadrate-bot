# Quadrate Bot — ПВЗ Озон возвраты

Telegram-бот для ПВЗ Озон: принимает скриншоты возвратов, распознаёт данные через GPT-4o mini Vision и записывает в Airtable.

## Стек

- Python 3.11+
- aiogram 3 (polling)
- OpenAI API — GPT-4o mini Vision
- Airtable REST API (httpx, без доп. библиотек)
- Railway — деплой

---

## Локальный запуск

```bash
git clone https://github.com/kmbytv/quadrate-bot.git
cd quadrate-bot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполни переменные
python bot/main.py
```

---

## Настройка Airtable

### 1. Создай базу

1. Зайди на [airtable.com](https://airtable.com) → **Add a base** → Start from scratch.
2. Назови базу как угодно.
3. Создай таблицу с названием **Возвраты** (или любым другим — запиши в `AIRTABLE_TABLE_NAME`).
4. Добавь поля (все тип **Single line text**):

| Название поля | Тип |
|---|---|
| Дата | Single line text |
| Время | Single line text |
| Сотрудник | Single line text |
| ID заказа | Single line text |
| Товар | Single line text |
| Причина | Single line text |

> Удали стандартное поле «Notes» и «Attachments» — они не нужны.

### 2. Получи Base ID

Открой базу в браузере — URL выглядит так:
```
https://airtable.com/appXXXXXXXXXXXXXX/tblYYYYYYYYYYYYYY/...
```
`appXXXXXXXXXXXXXX` — это твой `AIRTABLE_BASE_ID`.

### 3. Создай API токен

1. [airtable.com/create/tokens](https://airtable.com/create/tokens) → **Create new token**.
2. Название: `quadrate-bot`.
3. Scopes: добавь `data.records:write`.
4. Access: добавь свою базу.
5. Скопируй токен — он начинается с `pat...`.

---

## Деплой на Railway

### 1. Создай проект

[railway.app](https://railway.app) → New Project → Deploy from GitHub repo → выбери репозиторий.

### 2. Переменные окружения

В Railway → Variables добавь:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен от @BotFather |
| `OPENAI_API_KEY` | Ключ OpenAI |
| `AIRTABLE_API_KEY` | Personal Access Token (pat...) |
| `AIRTABLE_BASE_ID` | ID базы (app...) |
| `AIRTABLE_TABLE_NAME` | Название таблицы (Возвраты) |
| `DIRECTOR_CHAT_ID` | chat_id директора |
| `WHITELIST_IDS` | chat_id сотрудников через запятую |

### 3. Start Command

Railway → Settings → Deploy → Start Command:
```
python bot/main.py
```

### 4. Деплой

Railway задеплоит автоматически при пуше в `main`. В логах должно появиться:
```
Bot started. Polling...
```

---

## Структура проекта

```
bot/
  main.py       — запуск polling
  handlers.py   — хендлеры aiogram (FSM: скриншот → подтверждение → запись)
  vision.py     — вызов GPT-4o mini Vision, парсинг JSON
  sheets.py     — запись строки в Airtable через REST API
  config.py     — загрузка переменных из .env
.env.example
requirements.txt
README.md
```

---

## Поведение бота

1. Сотрудник из whitelist отправляет скриншот возврата.
2. Бот распознаёт: ID заказа, товар, причину, время (из статусбара телефона).
3. Показывает результат с кнопками **✅ Верно** / **✏️ Исправить**.
4. При исправлении принимает правки в формате `поле: значение`:
   - `order_id` / `id`
   - `product` / `товар`
   - `reason` / `причина`
   - `time` / `время`
5. После подтверждения — пишет запись в Airtable и уведомляет директора.
