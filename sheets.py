"""
sheets.py — запись разобранного отчёта в Google Sheets через SheetBest.

Структура новой таблицы (v1.1):
  • Сводка дня          — одна строка на отчёт (главный экран владельца)
  • Клиенты-сделки      — одна строка на каждого клиента
  • Операционные задачи — задачи не по клиентам (склад, поставщики, поручения)
  • Активности          — что делал продавец кроме продаж
"""

import os
import time
import logging
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

SHEETBEST_URL = os.getenv("SHEETBEST_URL")


def _today() -> str:
    """Текущая дата с апострофом — чтобы Sheets не пересчитывал как формулу."""
    return "'" + datetime.now().strftime("%d.%m.%Y")


def _today_plain() -> str:
    """Текущая дата без апострофа — для сравнения с данными из SheetBest."""
    return datetime.now().strftime("%d.%m.%Y")


def v(val, default="—"):
    """Значение для ячейки: None и пустое → заглушка, иначе строкой."""
    if val is None:
        return default
    s = str(val).strip()
    return s if s and s.lower() != "none" else default


def _post(tab: str, rows: list, retries: int = 4, delay: float = 2.0):
    """Шлёт ВСЕ строки во вкладку ОДНИМ запросом. При 429/402/5xx — повтор с паузой."""
    if not rows:
        return
    url = f"{SHEETBEST_URL}/tabs/{tab}"
    logger.info("POST %s — %d строк (батч)", url, len(rows))
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.post(url, json=rows, timeout=30)
            logger.info("SheetBest ответ %s: %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()
            return
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code not in (402, 429) and 400 <= code < 500:
                raise
            if attempt < retries:
                logger.warning("Попытка %d/%d для %s: HTTP %s — повтор через %.0fs",
                               attempt, retries, tab, code, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except Exception as e:
            if attempt < retries:
                logger.warning("Попытка %d/%d для %s: %s — повтор через %.0fs",
                               attempt, retries, tab, e, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise


def _fetch_tab(tab: str, retries: int = 3, delay: float = 2.0) -> list:
    """Читает вкладку с повтором. При окончательной ошибке возвращает []."""
    url = f"{SHEETBEST_URL}/tabs/{tab}"
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            if attempt < retries:
                logger.warning("Чтение %s, попытка %d/%d: %s — повтор через %.0fs",
                               tab, attempt, retries, e, delay)
                time.sleep(delay)
                delay *= 2
            else:
                logger.warning("Не удалось прочитать вкладку %s: %s", tab, e)
                return []
    return []


def daily_count_today(name: str) -> int:
    """Сколько отчётов этого сотрудника уже есть за сегодня в «Сводке дня»."""
    rows = _fetch_tab("Сводка дня")
    today = _today_plain()
    cnt = 0
    for row in rows:
        date_val = str(row.get("Дата", "")).lstrip("'")
        emp = str(row.get("Сотрудник", ""))
        if date_val == today and emp.startswith(name):
            cnt += 1
    return cnt


def write_all_sheets(name: str, data: dict, force: bool = False) -> dict:
    """
    Пишет данные во все вкладки.
      {"status": "ok", "report_no": N}  — записано
      {"status": "duplicate"}           — отчёт за сегодня уже есть

    force=True — записать несмотря на дубль (второй отчёт за день).
    """
    DATE = _today()

    daily = data.get("daily", {})
    clients = data.get("clients", [])
    tasks = data.get("tasks", [])
    operations = data.get("operations", [])

    existing = daily_count_today(name)
    if existing > 0 and not force:
        logger.warning("Отчёт от %s за сегодня уже есть (%d шт) — нужно подтверждение", name, existing)
        return {"status": "duplicate"}

    report_no = existing + 1
    emp_label = name if report_no == 1 else f"{name} (отчёт №{report_no})"

    # ── Клиенты-сделки ──
    seen, client_rows = set(), []
    for i, c in enumerate(clients, start=1):
        key = v(c.get("name"))
        if key in seen:
            continue
        seen.add(key)
        discount_val = c.get("discount")
        discount_str = f"{discount_val}%" if isinstance(discount_val, (int, float)) else v(discount_val)
        client_rows.append({
            "Дата": DATE,
            "Сотрудник": emp_label,
            "№ клиента": i,
            "Описание клиента": key,
            "Источник": v(c.get("source")),
            "Дизайнер": v(c.get("designer")),
            "Тип клиента": v(c.get("client_type")),
            "Категория товара": v(c.get("category")),
            "Конкретика (бренд / коллекция)": v(c.get("specifics")),
            "Площадь, м²": v(c.get("area")),
            "Этап воронки": v(c.get("stage")),
            "Сумма, ₽": v(c.get("amount")),
            "Способ оплаты": v(c.get("payment_method")),
            "Скидка, %": discount_str,
            "Причина скидки": v(c.get("discount_reason")),
            "Причина отказа": v(c.get("refusal_reason")),
            "Реакция клиента": v(c.get("client_reaction")),
            "Следующий шаг": v(c.get("next_step")),
            "Срок follow-up": v(c.get("followup_date")),
            "Статус follow-up": v(c.get("followup_status")),
        })

    # ── Операционные задачи ──
    seen, task_rows = set(), []
    for t in tasks:
        key = v(t.get("task"))
        if key in seen:
            continue
        seen.add(key)
        task_rows.append({
            "Дата создания": DATE,
            "Задача": key,
            "Связано с": v(t.get("related")),
            "Ответственный": v(t.get("responsible"), name),
            "Срок": v(t.get("deadline")),
            "Приоритет": v(t.get("priority")),
            "Статус": v(t.get("status")),
            "Комментарий": v(t.get("comment")),
        })

    # ── Активности ──
    seen, activity_rows = set(), []
    for op in operations:
        key = v(op.get("description"))
        if key in seen:
            continue
        seen.add(key)
        activity_rows.append({
            "Дата": DATE,
            "Сотрудник": emp_label,
            "Категория": v(op.get("category")),
            "Описание": key,
            "Связано с": v(op.get("related")),
            "Статус": v(op.get("status")),
            "Следующее действие": v(op.get("next_action")),
        })

    # Считаем производные метрики для сводки
    sold_clients = [c for c in clients if c.get("stage") == "Купил"]
    total = len(clients)
    sales_count = len(sold_clients)
    conversion = f"{round(sales_count / total * 100)}%" if total > 0 else "—"
    sold_amounts = [c["amount"] for c in sold_clients if isinstance(c.get("amount"), (int, float))]
    sales_amount = sum(sold_amounts) if sold_amounts else None
    avg_check = round(sales_amount / sales_count) if sales_amount and sales_count > 0 else None

    # Клиенты, задачи, активности — перед Сводкой дня
    _post("Клиенты-сделки", client_rows)
    _post("Операционные задачи", task_rows)
    _post("Активности", activity_rows)

    # Сводка дня — ПОСЛЕДНЕЙ (по ней определяется дубль)
    _post("Сводка дня", [{
        "Дата": DATE,
        "Сотрудник": emp_label,
        "Клиентов всего": v(total if total > 0 else None),
        "Купили": v(sales_count),
        "Конверсия %": conversion,
        "КП / счета": v(daily.get("kp")),
        "Сумма продаж ₽": v(sales_amount),
        "Средний чек": v(avg_check),
        "Главное событие": v(daily.get("main_event")),
        "Наблюдение дня": v(daily.get("observation")),
        "Потенциальные сделки": v(daily.get("potential")),
        "Follow-up на завтра": v(daily.get("followup")),
        "Операционные задачи (итог)": v(daily.get("tasks")),
    }])

    logger.info("Записано: сотрудник=%s, отчёт №%d", emp_label, report_no)
    return {"status": "ok", "report_no": report_no}
