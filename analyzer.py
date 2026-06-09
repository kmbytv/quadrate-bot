"""
analyzer.py — ядро разбора голосового отчёта продавца.

Ключевые идеи (зачем так, а не как было раньше):

1. ПРАВИЛА-ПО-ДЕЙСТВИЮ, а не справочник имён.
   Бот не знает заранее, кто поставщик, а кто клиент. Поэтому решает по
   ДЕЙСТВИЮ: "привезли на склад" → операция; "купил / приехал забрать своё /
   позвонил узнать" → клиент. Так бот работает у любого заказчика без донастройки.
   Опциональный справочник имён можно задать через .env (KNOWN_SUPPLIERS,
   KNOWN_STAFF) — он лишь ПОДСКАЗКА-приоритет, не жёсткий закон.

2. РАЗВЯЗАННАЯ СВЕРКА.
   Главный баг прошлой версии: все три "источника" проверки брались из ОДНОГО
   прохода модели и ошибались синхронно. Теперь второй счётчик клиентов делает
   НЕЗАВИСИМЫЙ проход (count_people), который занят ровно одним делом — пересчётом
   живых контактов. Если его число расходится с массивом clients[] — повторяем
   разбор с явным фидбэком.

3. MAP-REDUCE для длинных отчётов.
   Длинный плотный текст модель "схлопывает" в середине. Поэтому длинный отчёт
   бьётся на куски по предложениям, каждый кусок разбирается отдельно (map),
   затем результаты сливаются с дедупом (reduce). Короткий отчёт идёт в один
   проход — быстро и дёшево.
"""

import os
import re
import json
import logging
import httpx

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
PROXY_URL = os.getenv("PROXY_URL")

# Модель-агностично: ставь любую сильную reasoning-модель через .env.
MODEL = os.getenv("LLM_MODEL", "anthropic/claude-opus-4-5")

# Порог, после которого включается map-reduce. Меряем в символах транскрипта.
# ~1800 символов ≈ полторы минуты речи. Длиннее — бьём на куски.
LONG_REPORT_CHARS = int(os.getenv("LONG_REPORT_CHARS", "1800"))
# Целевой размер одного куска при map-reduce (символы).
CHUNK_TARGET_CHARS = int(os.getenv("CHUNK_TARGET_CHARS", "1400"))
# Сколько раз пытаться добиться схождения сверки клиентов.
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))

# Опциональные справочники (по умолчанию пустые — логика держится на правилах).
KNOWN_SUPPLIERS = [s.strip() for s in os.getenv("KNOWN_SUPPLIERS", "").split(",") if s.strip()]
KNOWN_STAFF = [s.strip() for s in os.getenv("KNOWN_STAFF", "").split(",") if s.strip()]


def _headers():
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }


def _client():
    return httpx.Client(proxy=PROXY_URL, timeout=120) if PROXY_URL else httpx.Client(timeout=120)


def _hint_block() -> str:
    """Собирает опциональную подсказку-справочник, если заданы имена в .env."""
    parts = []
    if KNOWN_SUPPLIERS:
        parts.append("Известные ПОСТАВЩИКИ (их привоз на склад = операция, не клиент): "
                     + ", ".join(KNOWN_SUPPLIERS) + ".")
    if KNOWN_STAFF:
        parts.append("Известные СОТРУДНИКИ (не клиенты, их поручения = задачи): "
                     + ", ".join(KNOWN_STAFF) + ".")
    if not parts:
        return ""
    return ("\n━━━ ПОДСКАЗКА ПО ИМЕНАМ (приоритет, но не закон) ━━━\n"
            + "\n".join(parts)
            + "\nЕсли имя НЕ в списках — определяй роль ПО ДЕЙСТВИЮ, а не по имени.\n")


# ─────────────────────────────────────────────────────────────────────────────
#  ПРОМПТЫ
# ─────────────────────────────────────────────────────────────────────────────

# Общая часть с правилами — переиспользуется в обоих режимах.
RULES = """━━━ КТО ТАКОЙ КЛИЕНТ (определяй ПО ДЕЙСТВИЮ, не по имени) ━━━
Клиент — это человек, который СЕГОДНЯ лично взаимодействовал с магазином ради покупки:
• зашёл в магазин (даже просто посмотреть / взять образцы)
• позвонил или написал в мессенджер по поводу товара
• приехал забрать СВОЙ заказ
• повторный клиент или клиент с докупкой — это ОТДЕЛЬНАЯ запись
КАЖДАЯ парочка, КАЖДЫЙ отдельный заход = отдельная запись в clients[].
Две парочки за день = две записи. Не схлопывай похожих клиентов в одного.

━━━ КТО НЕ КЛИЕНТ ━━━
• ПРИВОЗ товара на склад/в магазин (кто-то "привёз", "доставили", "разгрузил") —
  это ОПЕРАЦИЯ (operations[]), даже если назван получатель и даже если имя похоже
  на клиента. Ключ — действие "привезли на склад", а не имя.
• СОТРУДНИК / коллега, который дал поручение или передал информацию — сам он не
  клиент. Его поручение идёт в tasks[].
Правило-разделитель: "товар приехал К НАМ на склад" = операция;
"человек пришёл/позвонил К НАМ покупать" = клиент.

━━━ ЧТО СЧИТАТЬ ПРОДАЖЕЙ (sales) ━━━
Продажа = СЕГОДНЯ зафиксирована ОПЛАТА (карта / наличные / перевод).
НЕ продажа: расчёт/КП без оплаты, "думает", "взял образцы", follow-up,
будущая отгрузка без оплаты. sales = число клиентов, реально оплативших сегодня.

━━━ ЧИСЛА ━━━
• Суммы — ТОЛЬКО явно названные в тексте. Ничего не вычисляй и не придумывай.
• Сумма не названа → null (именно null, не "—", не 0, не строка).
• Числа — как JSON number: 3, 165000. Не строки.
• Площадь пола — в КВАДРАТНЫХ метрах (м²). Пол НЕ меряют в кубометрах.

━━━ ДАТЫ ━━━
• НЕ бери дату из текста. Дату проставляет система автоматически.
• followup_date — относительное словом: "завтра"/"послезавтра"/"через неделю"/
  "через месяц"/null.

━━━ ЗАПРЕЩЕНО ━━━
• придумывать клиентов, суммы, детали, которых нет в тексте
• писать "—" или "null"-строку вместо настоящего null
• копировать данные из примеров
""" + _hint_block()


# Поля JSON — единый контракт для обоих режимов.
SCHEMA = """━━━ СТРУКТУРА JSON (верни ТОЛЬКО валидный JSON, без текста и без ``` вокруг) ━━━
{
  "raw_events": [строки],   // ПЕРВЫМ заполняешь: каждое событие текста = строка
                            // с пометкой типа (КЛИЕНТ)/(ЗАДАЧА)/(ОПЕРАЦИЯ)/(ИНФО)
  "daily": {
    "clients": int,            // = длине clients[] (проставит система, но верни своё)
    "sales": int,              // число клиентов, оплативших сегодня
    "sales_amount": int|null,  // сумма рублей, только если суммы явно названы
    "kp": int,                 // число расчётов/КП (с суммой, но без оплаты)
    "potential": "строка",     // кто и что может купить, с деталями
    "followup": "строка",      // кому и когда перезвонить/написать/привезти
    "tasks": "строка",         // что сделано и что осталось
    "summary": "строка"        // 2-3 предложения с цифрами и именами
  },
  "clients": [{
    "name": "имя или описание ('парочка, клеевой пол 10 м²')",
    "source": "сам зашёл / повторный / звонок / мессенджер / неизвестно",
    "interest": "конкретно что смотрел/купил/спрашивал — товар, размер, нюансы",
    "area": int|null,          // площадь в м²
    "amount": int|null,        // рублей, если названа
    "discount": "решение по скидке ('отменили 10%', '10% согласовано') или null",
    "status": "купил / взял образцы / получил расчёт / думает / забрал материал / отгрузка",
    "next_step": "конкретный следующий шаг или null",
    "followup_date": "завтра / послезавтра / через неделю / через месяц / null",
    "comment": "ВСЁ важное: кто направил, способ оплаты, договорённости, нюансы / null"
  }],
  "tasks": [{
    "task": "описание",
    "related": "с чем/кем связано",
    "responsible": "имя или 'продавец'",
    "deadline": "срок или null",
    "priority": "Высокий / Средний / Низкий",
    "status": "Новая / В работе / Выполнено / Ожидаем ответ",
    "comment": "или null"
  }],
  "operations": [{
    "category": "Доставка / Логистика / Склад / Отгрузка / Мерчандайзинг / Заказ",
    "description": "что произошло",
    "related": "клиент или контрагент",
    "status": "Выполнено / Подготовлено / Подтверждено / Новая",
    "next_action": "следующий шаг или null"
  }]
}
clients, tasks, operations, raw_events — всегда массивы, могут быть пустыми [].
Стремись к МАКСИМУМУ деталей в каждом поле: всё, что названо в тексте, должно
попасть в comment/interest/next_step. Лучше длинный честный comment, чем null."""


SYSTEM_PROMPT = f"""Ты — ассистент руководителя магазина напольных покрытий.
Тебе дают расшифровку голосового отчёта продавца за день. Твоя задача —
разложить её в строгий JSON, НЕ ПОТЕРЯВ НИ ОДНОГО человека и НИ ОДНОЙ детали.

═══ ПОРЯДОК РАБОТЫ ═══
ШАГ 1. Пройди текст СВЕРХУ ВНИЗ и выпиши КАЖДОЕ событие в raw_events[] с пометкой
       типа. Это твой чек-лист. Сколько (КЛИЕНТ) в raw_events — столько записей
       обязано быть в clients[].
ШАГ 2. Разложи события по массивам clients/tasks/operations по правилам ниже.
ШАГ 3. Перед выдачей пересчитай: число (КЛИЕНТ) в raw_events == длине clients[].
       Если не сходится — ты кого-то потерял, вернись и добавь. Частые потеряшки:
       вторая парочка, клиент в самом конце отчёта.

{RULES}

{SCHEMA}"""


# Few-shot: показываем образцовый разбор. Две парочки + докупка + поставка +
# задача от сотрудника — ровно тот случай, где старая версия теряла людей.
FEWSHOT_USER = """Отчёт продавца:
Сначала зашла парочка, нужен клеевой пол на 30 квадратов, посчитал расчёт на 95000,
взяли образцы, думают. Потом позвонил Игорь — спрашивал про паркетную доску, зайдёт
через месяц. Пришла клиентка, не хватило плитки, докупила 2 упаковки, оплатила картой,
сделал доверенность, завтра поедет на склад. Привезли со склада 15 упаковок для Сергея,
разгрузил в подсобку. Ещё зашла парочка под тёплый пол, купили 2 пачки плитки, оплатили,
сразу забрали. Вечером Маша сказала — надо согласовать скидку для VIP с директором, срочно."""

FEWSHOT_ASSISTANT = json.dumps({
    "raw_events": [
        "Парочка №1 — клеевой пол 30 м², расчёт 95000, образцы, думают (КЛИЕНТ)",
        "Игорь позвонил — паркетная доска, зайдёт через месяц (КЛИЕНТ)",
        "Клиентка — докупка 2 упаковки плитки, оплата картой, доверенность на завтра (КЛИЕНТ)",
        "Привоз 15 упаковок для Сергея со склада в подсобку (ОПЕРАЦИЯ, не клиент)",
        "Парочка №2 — тёплый пол, 2 пачки плитки, оплатили, забрали (КЛИЕНТ)",
        "Маша: согласовать скидку VIP с директором, срочно (ЗАДАЧА)"
    ],
    "daily": {
        "clients": 4,
        "sales": 2,
        "sales_amount": None,
        "kp": 1,
        "potential": "Парочка №1 — клеевой пол 30 м², расчёт 95000, взяли образцы; Игорь — паркетная доска, через месяц",
        "followup": "Клиентка — завтра на склад по доверенности; Игорь — через месяц",
        "tasks": "Согласовать скидку VIP с директором (срочно); привоз 15 упаковок для Сергея выполнен",
        "summary": "4 контакта, 2 продажи (докупка плитки и тёплый пол, обе картой). Парочка №1 получила расчёт 95000, думает. Привоз 15 упаковок для Сергея — в подсобке."
    },
    "clients": [
        {"name": "Парочка №1, клеевой пол 30 м²", "source": "сам зашёл",
         "interest": "клеевой пол 30 м², расчёт 95000", "area": 30, "amount": 95000,
         "discount": None, "status": "взял образцы", "next_step": "ждём решения по расчёту",
         "followup_date": None, "comment": "взяли образцы, думают"},
        {"name": "Игорь", "source": "звонок", "interest": "паркетная доска, уточнял наличие",
         "area": None, "amount": None, "discount": None, "status": "думает",
         "next_step": "зайдёт смотреть через месяц", "followup_date": "через месяц", "comment": None},
        {"name": "Клиентка, докупка плитки", "source": "повторный",
         "interest": "не хватило плитки — докупила 2 упаковки", "area": None, "amount": None,
         "discount": None, "status": "купил", "next_step": "заберёт завтра по доверенности",
         "followup_date": "завтра", "comment": "оплата картой, оформлена доверенность на склад"},
        {"name": "Парочка №2, тёплый пол", "source": "сам зашёл",
         "interest": "плитка под тёплый пол, 2 пачки", "area": None, "amount": None,
         "discount": None, "status": "купил", "next_step": None, "followup_date": None,
         "comment": "оплатили, сразу забрали"}
    ],
    "tasks": [
        {"task": "согласовать скидку для VIP-клиента с директором", "related": "VIP-клиент",
         "responsible": "продавец", "deadline": None, "priority": "Высокий", "status": "Новая",
         "comment": "Маша сказала — срочно"}
    ],
    "operations": [
        {"category": "Склад", "description": "привоз 15 упаковок для Сергея, размещены в подсобке",
         "related": "Сергей", "status": "Выполнено", "next_action": None}
    ]
}, ensure_ascii=False)


# Промпт независимого счётчика людей — второй, РАЗВЯЗАННЫЙ источник правды.
# Делает ровно одно: считает живые контакты. Не раскладывает, не отвлекается.
COUNT_SYSTEM = """Ты считаешь КЛИЕНТОВ в отчёте продавца магазина напольных покрытий.

Клиент = человек/группа, который СЕГОДНЯ лично контактировал с магазином ради
покупки: зашёл, позвонил, написал, приехал забрать свой заказ. Каждая отдельная
парочка и каждый отдельный заход = +1. Повторный клиент и докупка = тоже +1.

НЕ клиент: привоз товара на склад (это поставка), сотрудник/коллега с поручением.

Пройди текст сверху вниз, выпиши КАЖДЫЙ контакт отдельной строкой, затем верни
ТОЛЬКО JSON без текста вокруг:
{"people": ["краткое описание каждого клиента"], "count": число}"""


# ─────────────────────────────────────────────────────────────────────────────
#  НИЗКОУРОВНЕВЫЙ ВЫЗОВ МОДЕЛИ
# ─────────────────────────────────────────────────────────────────────────────

def _call_llm(messages: list, max_tokens: int = 8000, temperature: float = 0.1) -> str:
    """Один вызов модели. Возвращает очищенный от ``` текст ответа."""
    with _client() as client:
        resp = client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=_headers(),
            json={
                "model": MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    return raw


def _parse_json(raw: str) -> dict:
    """Безопасный парсинг. При мусоре пытается выдрать первый {...} блок."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError("Модель вернула невалидный JSON")


# ─────────────────────────────────────────────────────────────────────────────
#  НЕЗАВИСИМЫЙ СЧЁТЧИК ЛЮДЕЙ (развязанный источник правды)
# ─────────────────────────────────────────────────────────────────────────────

def count_people(text: str) -> int:
    """Отдельный проход модели, считающий только живых клиентов.
    Это ВТОРОЙ независимый источник — он не знает про разбор и не ошибается
    синхронно с основным проходом. Возвращает -1, если посчитать не удалось."""
    try:
        raw = _call_llm(
            [
                {"role": "system", "content": COUNT_SYSTEM},
                {"role": "user", "content": f"Отчёт продавца:\n{text}"},
            ],
            max_tokens=1500,
        )
        data = _parse_json(raw)
        people = data.get("people")
        count = data.get("count")
        if isinstance(people, list) and people:
            return len(people)
        if isinstance(count, int):
            return count
    except Exception as e:
        logger.warning("Независимый счётчик людей не сработал: %s", e)
    return -1


# ─────────────────────────────────────────────────────────────────────────────
#  РАЗБОР ОДНОГО КУСКА / КОРОТКОГО ОТЧЁТА (map-фаза)
# ─────────────────────────────────────────────────────────────────────────────

def _analyze_once(text: str, feedback: str | None = None, is_chunk: bool = False) -> dict:
    """Один проход разбора. feedback — текст коррекции при повторной попытке."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": FEWSHOT_USER},
        {"role": "assistant", "content": FEWSHOT_ASSISTANT},
    ]
    if is_chunk:
        messages.append({
            "role": "user",
            "content": ("Это ФРАГМЕНТ длинного отчёта (не начало и не конец целого). "
                        "Разбери только то, что есть в этом фрагменте, по той же схеме. "
                        "Не придумывай недостающий контекст.\n\nФрагмент:\n" + text),
        })
    else:
        messages.append({"role": "user", "content": f"Отчёт продавца:\n{text}"})
    if feedback:
        messages.append({"role": "user", "content": feedback})

    raw = _call_llm(messages)
    return _parse_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
#  MAP-REDUCE для длинных отчётов
# ─────────────────────────────────────────────────────────────────────────────

def _split_into_chunks(text: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    """Бьёт текст на куски по границам предложений, не разрывая предложение.
    Куски примерно равны target по длине."""
    # Режем по концам предложений (., !, ?) с пробелом после.
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > target:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur.strip():
        chunks.append(cur.strip())
    return chunks or [text]


def _dedup_clients(clients: list) -> list:
    """Дедуп клиентов на reduce-фазе: один клиент мог попасть на стык двух кусков.
    Дубль = совпадает нормализованное имя И заметно пересекаются слова interest.
    Сравнение по пересечению слов устойчивее, чем срез по символам
    ('клеевой пол 30 м2' и 'клеевой пол 30 квадратов' — это один клиент)."""
    STOP = {"и", "в", "на", "по", "от", "для", "с", "м2", "м²", "квадратов",
            "квадрата", "кв", "штук", "упаковки", "упаковок", "пачки", "пачек"}

    def norm(s):
        return re.sub(r"\s+", " ", str(s or "").lower()).strip()

    def words(s):
        return {w for w in re.findall(r"\w+", norm(s)) if w not in STOP and len(w) > 2}

    seen, result = [], []
    for c in clients:
        raw_nm = c.get("name")
        # None-имя нормализуется в "" — не сравниваем пустые имена между собой,
        # иначе все анонимные клиенты схлопнутся в одного.
        nm = norm(raw_nm) if raw_nm else None
        iw = words(c.get("interest"))
        dup = False
        for snm, siw in seen:
            if not nm or not snm or nm != snm:
                continue
            # Имена совпали. Если interest пустой у кого-то — считаем дублем.
            # Иначе требуем существенное пересечение слов (≥50% меньшего набора).
            if not iw or not siw:
                dup = True
                break
            overlap = len(iw & siw)
            # ceil(min/2) — порог ≥50% меньшего набора без truncation-ошибки.
            min_sz = min(len(iw), len(siw))
            if overlap >= max(1, (min_sz + 1) // 2):
                dup = True
                break
        if dup:
            continue
        seen.append((nm, iw))
        result.append(c)
    return result


def _dedup_by(items: list, field: str) -> list:
    """Простой дедуп задач/операций по ключевому полю."""
    seen, result = set(), []
    for it in items:
        key = re.sub(r"\s+", " ", str(it.get(field) or "").lower()).strip()
        if key and key in seen:
            continue
        seen.add(key)
        result.append(it)
    return result


def _merge_chunks(parts: list[dict]) -> dict:
    """Reduce: сливает разборы кусков в один результат с дедупом."""
    clients, tasks, operations, raw_events = [], [], [], []
    for p in parts:
        clients += p.get("clients") or []
        tasks += p.get("tasks") or []
        operations += p.get("operations") or []
        raw_events += p.get("raw_events") or []

    clients = _dedup_clients(clients)
    tasks = _dedup_by(tasks, "task")
    operations = _dedup_by(operations, "description")

    # daily собираем агрегатами из слитых массивов, текстовые поля — склейкой.
    def join_field(key):
        vals = [str((p.get("daily") or {}).get(key)).strip()
                for p in parts
                if (p.get("daily") or {}).get(key)]
        vals = [v for v in vals if v and v.lower() != "none"]
        return "; ".join(dict.fromkeys(vals)) or None  # dict.fromkeys убирает повторы

    daily = {
        "clients": len(clients),
        # Продажа = клиент оплатил (статус "купил"), сумма может отсутствовать.
        "sales": sum(1 for c in clients if str(c.get("status", "")).strip().startswith("куп")),
        "sales_amount": None,  # суммы агрегировать опасно — пусть проставит финальный проход ниже
        # КП = расчёты с суммой, но без оплаты (статус НЕ "купил").
        "kp": sum(1 for c in clients if c.get("amount") and not str(c.get("status", "")).strip().startswith("куп")),
        "potential": join_field("potential"),
        "followup": join_field("followup"),
        "tasks": join_field("tasks"),
        "summary": join_field("summary"),
    }
    return {
        "raw_events": raw_events,
        "daily": daily,
        "clients": clients,
        "tasks": tasks,
        "operations": operations,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ВАЛИДАЦИЯ И ФОРСИРОВАНИЕ ИНВАРИАНТОВ
# ─────────────────────────────────────────────────────────────────────────────

def validate_and_fix(data: dict, independent_count: int = -1) -> dict:
    """Форсим инварианты, ловим галлюцинации и пропуски.
    independent_count — число клиентов от РАЗВЯЗАННОГО счётчика (или -1)."""
    if not isinstance(data, dict):
        return {}

    daily = data.get("daily") or {}
    clients = data.get("clients") if isinstance(data.get("clients"), list) else []
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    operations = data.get("operations") if isinstance(data.get("operations"), list) else []
    raw_events = data.get("raw_events") if isinstance(data.get("raw_events"), list) else []

    warnings = []
    in_array = len(clients)

    # Источники оценки числа клиентов:
    events_upper = [str(e).upper() for e in raw_events]
    client_events = sum(1 for e in events_upper if "(КЛИЕНТ)" in e or "(CLIENT)" in e)
    # independent_count — НЕЗАВИСИМЫЙ, главный источник против синхронной ошибки.
    candidates = [in_array, client_events]
    if independent_count >= 0:
        candidates.append(independent_count)
    expected = max(candidates)

    if expected > in_array:
        msg = (f"ПРОПУЩЕН клиент: ожидалось {expected} "
               f"(независимый счётчик {independent_count}, метки {client_events}), "
               f"в таблицу попало {in_array}.")
        logger.warning("⚠️ %s", msg)
        warnings.append(msg)

    # daily.clients ВСЕГДА = факту в массиве (не врём в большую сторону).
    daily["clients"] = in_array

    # KP по клиентам с суммой, если модель не заполнила.
    if not daily.get("kp"):
        daily["kp"] = sum(1 for c in clients if c.get("amount"))

    # Галлюцинация суммы: > 10 млн — почти наверняка выдумано.
    if isinstance(daily.get("sales_amount"), (int, float)) and daily["sales_amount"] > 10_000_000:
        logger.warning("⚠️ Подозрительная сумма %s — обнуляем", daily["sales_amount"])
        daily["sales_amount"] = None

    # Продаж не может быть больше клиентов.
    if isinstance(daily.get("sales"), int) and daily["sales"] > in_array:
        warnings.append(f"продаж {daily['sales']} > клиентов {in_array} — проверь")

    # Полнота задач/операций по меткам raw_events.
    if raw_events:
        task_events = sum(1 for e in events_upper if "(ЗАДАЧА)" in e)
        op_events = sum(1 for e in events_upper if "(ОПЕРАЦИЯ)" in e)
        if task_events and task_events != len(tasks):
            warnings.append(f"задач: меток {task_events} ≠ записей {len(tasks)}")
        if op_events and op_events != len(operations):
            warnings.append(f"операций: меток {op_events} ≠ записей {len(operations)}")

    # Чистим строки-заглушки → None во всех клиентских полях.
    for c in clients:
        for k, v in list(c.items()):
            if isinstance(v, str) and v.strip() in ("—", "null", ""):
                c[k] = None

    data.update({
        "daily": daily, "clients": clients, "tasks": tasks,
        "operations": operations, "raw_events": raw_events,
        "_warnings": warnings,
    })
    return data


def _missing_clients(d: dict) -> bool:
    return any("ПРОПУЩЕН клиент" in w for w in d.get("_warnings", []))


# ─────────────────────────────────────────────────────────────────────────────
#  ГЛАВНАЯ ТОЧКА ВХОДА
# ─────────────────────────────────────────────────────────────────────────────

def analyze_report(text: str) -> dict:
    """Разбирает отчёт. Сам выбирает режим (короткий проход / map-reduce),
    использует независимый счётчик людей и повторяет при пропусках."""
    text = (text or "").strip()
    if not text:
        return {}

    is_long = len(text) > LONG_REPORT_CHARS
    logger.info("Режим разбора: %s (%d символов)",
                "MAP-REDUCE (длинный)" if is_long else "один проход (короткий)", len(text))

    # Независимый счётчик считаем ВСЕГДА — это наш развязанный контроль.
    independent = count_people(text)
    logger.info("Независимый счётчик клиентов: %s", independent)

    best = {}
    for attempt in range(MAX_ATTEMPTS):
        try:
            if is_long:
                chunks = _split_into_chunks(text)
                logger.info("Попытка %d: разбито на %d кусков", attempt + 1, len(chunks))
                # При повторе из-за пропущенного клиента передаём feedback в каждый кусок.
                chunk_feedback = None
                if attempt > 0 and best and _missing_clients(best):
                    chunk_feedback = (
                        f"В прошлый раз ПОТЕРЯН клиент. Независимый подсчёт даёт "
                        f"{independent} клиентов. Перечитай фрагмент СВЕРХУ ВНИЗ, "
                        "не пропусти ни одного контакта."
                    )
                parts = []
                for i, ch in enumerate(chunks):
                    parts.append(_analyze_once(ch, feedback=chunk_feedback, is_chunk=True))
                    logger.info("  кусок %d/%d разобран", i + 1, len(chunks))
                data = _merge_chunks(parts)
            else:
                feedback = None
                if attempt > 0 and best and _missing_clients(best):
                    feedback = (
                        f"В прошлый раз ты ПОТЕРЯЛ клиента. Независимый подсчёт даёт "
                        f"{independent} клиентов, а ты вернул {len(best.get('clients', []))}. "
                        "Перечитай отчёт СВЕРХУ ВНИЗ, найди КАЖДУЮ парочку и КАЖДЫЙ заход "
                        "(особенно в конце) и верни ПОЛНЫЙ JSON, где каждый клиент есть в clients[]."
                    )
                data = _analyze_once(text, feedback=feedback)

            data = validate_and_fix(data, independent_count=independent)
            best = data

            if not _missing_clients(data):
                logger.info("Попытка %d: сверка сошлась, %d клиентов",
                            attempt + 1, len(data.get("clients", [])))
                return data
            logger.warning("Попытка %d: пропуск клиента, повтор", attempt + 1)

        except Exception as e:
            logger.error("Ошибка разбора (попытка %d): %s", attempt + 1, e)

    # Все попытки исчерпаны — отдаём лучшее, но с честным флагом для UI.
    if best:
        best.setdefault("_warnings", [])
        if _missing_clients(best):
            best["_unresolved"] = True  # bot.py покажет это продавцу заметно
    return best
