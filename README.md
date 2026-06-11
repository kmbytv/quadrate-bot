# Quadrate Bot — ПВЗ Озон возвраты

Telegram-бот для ПВЗ Озон: принимает скриншоты возвратов, распознаёт данные через GPT-4o mini Vision и записывает в Google Sheets.

## Стек

- Python 3.11+
- aiogram 3 (polling)
- OpenAI API — GPT-4o mini Vision
- gspread + google-auth — Google Sheets API
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

## Настройка Google Sheets

1. Зайди в [Google Cloud Console](https://console.cloud.google.com/).
2. Создай проект → API & Services → Enable APIs → включи **Google Sheets API** и **Google Drive API**.
3. Создай Service Account: IAM & Admin → Service Accounts → Create → выдай роль *Editor*.
4. Создай ключ: Keys → Add Key → JSON → скачай файл.
5. Открой Google Таблицу и добавь email сервис-аккаунта (из `client_email` в JSON) как редактора.
6. Скопируй содержимое JSON-файла **в одну строку** и вставь в переменную `GOOGLE_CREDENTIALS_JSON`.

Преобразовать в одну строку (bash):
```bash
cat credentials.json | tr -d '\n' | sed 's/\\n/\\\\n/g'
```

---

## Деплой на Railway

### 1. Подготовка

- Убедись, что в репозитории нет файла `.env` (только `.env.example`).

### 2. Создание проекта

1. Зайди на [railway.app](https://railway.app) → New Project → Deploy from GitHub repo.
2. Выбери репозиторий `quadrate-bot`.

### 3. Переменные окружения

В Railway → Variables добавь все переменные из `.env.example`:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `OPENAI_API_KEY` | ключ OpenAI |
| `GOOGLE_CREDENTIALS_JSON` | содержимое credentials.json одной строкой |
| `SPREADSHEET_ID` | ID Google Таблицы |
| `DIRECTOR_CHAT_ID` | chat_id директора |
| `WHITELIST_IDS` | chat_id сотрудников через запятую |

### 4. Start Command

В Railway → Settings → Deploy → Start Command:
```
python bot/main.py
```

### 5. Деплой

Railway автоматически задеплоит при пуше в `main`. Проверь логи — должно быть:
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
  sheets.py     — запись строки в Google Sheets
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
4. При исправлении — принимает текстовые правки в формате `поле: значение`.
5. После подтверждения — пишет строку в Google Sheets и уведомляет директора.

### Структура таблицы

| Дата | Время | Сотрудник | ID заказа | Товар | Причина |
|---|---|---|---|---|---|
