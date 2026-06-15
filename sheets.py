"""
sheets.py — запись разобранного отчёта в Google Sheets через SheetBest.
"""

import os
import time
import logging
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

SHEETBEST_URL = os.getenv("SHEETBEST_URL")
ENABLE_SALES_SUMMARY = os.getenv("ENABLE_SALES_SUMMARY", "false").lower() == "true"


def _today() -> str:
    """Текущая дата как текст для ячейки (апостроф — чтобы Sheets не пересчитывал)."""
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


def _post(tab: str, rows: list, retries: int = 3, delay: float = 2.0):
    """Шлёт строки в одну вкладку. При 429/402/5xx повторяет с паузой."""
    if not rows:
        return
    url = f"{SHEETBEST_URL}/tabs/{tab}"
    logger.info("POST %s — %d строк", url, len(rows))
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.post(url, json=rows, timeout=30)
            logger.info("SheetBest ответ %s: %s", resp.status_code, resp.text[:200])
            resp.raise_for_status()
            return
        except httpx.HTTPStatusError as e:
            last_err = e
            if attempt < retries:
                logger.warning("Попытка %d/%d для %s: %s — повтор через %.0fs",
                               attempt, retries, tab, e.response.status_code, delay)
                time.sleep(delay)
                delay *= 2
            else:
                raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                logger.warning("Попытка %d/%d для %s: %s — повтор", attempt, retries, tab, e)
                time.sleep(delay)
                delay *= 2
            else:
                raise


def _fetch_tab(tab: str) -> list:
    """Читает вкладку. При любой ошибке возвращает [] — чтобы не блокировать запись."""
    try:
        resp = httpx.get(f"{SHEETBEST_URL}/tabs/{tab}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Не удалось прочитать вкладку %s: %s", tab, e)
        return []


def daily_count_today(name: str) -> int:
    """Сколько отчётов этого сотрудника уже есть за сегодня в Daily Reports."""
    rows = _fetch_tab("Daily Reports")
    today = _today_plain()  # без апострофа — SheetBest возвращает без него
    cnt = 0
    for row in rows:
        date_val = str(row.get("Дата", "")).lstrip("'")  # убираем апостроф если есть
        emp = str(row.get("Сотрудник", ""))
        if date_val == today and emp.startswith(name):
            cnt += 1
    return cnt


def write_all_sheets(name: str, data: dict, force: bool = False) -> dict:
    """
    Пишет данные во все вкладки.
      {"status": "ok", "report_no": N}  — записано
      {"status": "duplicate"}           — отчёт за сегодня уже есть, нужно подтверждение

    force=True — записать несмотря на уже существующий отчёт (второй отчёт за день).
    """
    DATE = _today()  # вычисляем здесь, а не при старте модуля

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

    # ── Daily Reports — одна строка на отчёт ──
    _post("Daily Reports", [{
        "Дата": DATE,
        "Сотрудник": emp_label,
        "Клиентов за день": v(daily.get("clients")),
        "Продаж": v(daily.get("sales")),
        "Сумма продаж": v(daily.get("sales_amount")),
        "КП / счета": v(daily.get("kp")),
        "Потенциальные сделки": v(daily.get("potential")),
        "Follow-up": v(daily.get("followup")),
        "Операционные задачи": v(daily.get("tasks")),
        "Итог дня": v(daily.get("summary")),
    }])

    # ── Clients Leads — все клиенты, дедуп внутри отчёта по имени ──
    seen, client_rows = set(), []
    for c in clients:
        key = v(c.get("name"))
        if key in seen:
            continue
        seen.add(key)
        client_rows.append({
            "Дата": DATE,
            "Сотрудник": emp_label,
            "Клиент": key,
            "Источник": v(c.get("source")),
            "Что интересовало": v(c.get("interest")),
            "Площадь / объем": v(c.get("area")),
            "Сумма": v(c.get("amount")),
            "Скидка": v(c.get("discount")),
            "Статус": v(c.get("status")),
            "Следующий шаг": v(c.get("next_step")),
            "Срок follow-up": v(c.get("followup_date")),
            "Комментарий": v(c.get("comment")),
        })
    for row in client_rows:
        _post("Clients Leads", [row])

    # ── Tasks Follow-up ──
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
    for row in task_rows:
        _post("Tasks Follow-up", [row])

    # ── Issues Operations ──
    seen, op_rows = set(), []
    for op in operations:
        key = v(op.get("description"))
        if key in seen:
            continue
        seen.add(key)
        op_rows.append({
            "Дата": DATE,
            "Сотрудник": emp_label,
            "Категория": v(op.get("category")),
            "Описание": key,
            "Связано с": v(op.get("related")),
            "Статус": v(op.get("status")),
            "Следующее действие": v(op.get("next_action")),
        })
    for row in op_rows:
        _post("Issues Operations", [row])

    if ENABLE_SALES_SUMMARY:
        try:
            update_sales_summary(name, daily, DATE)
        except Exception as e:
            logger.warning("Sales Summary не обновлён: %s", e)

    logger.info("Записано: сотрудник=%s, отчёт №%d", emp_label, report_no)
    return {"status": "ok", "report_no": report_no}


def update_sales_summary(name: str, daily: dict, DATE: str = None):
    if DATE is None:
        DATE = _today()
    _post("Sales Summary", [{
        "Дата": DATE,
        "Сотрудник": name,
        "Клиентов": v(daily.get("clients")),
        "Продаж": v(daily.get("sales")),
        "Сумма": v(daily.get("sales_amount")),
        "КП": v(daily.get("kp")),
    }])
