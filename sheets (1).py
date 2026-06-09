"""
sheets.py — запись разобранного отчёта в Google Sheets через SheetBest.

Что изменилось против старой версии:
• Два отчёта за день теперь ДОПОЛНЯЮТ день, а не затирают. daily_exists остаётся
  как защита от СЛУЧАЙНОГО двойного нажатия, но bot.py предлагает кнопку
  "это второй отчёт — добавить", и тогда мы пишем с force=True без потери первого.
• Daily Reports при втором отчёте за день не плодит дубль-строку, а помечает
  строку признаком "отчёт №2" в поле сотрудника (видно в таблице).
• Sales Summary — задел: функция update_sales_summary() готова, но по умолчанию
  выключена (ENABLE_SALES_SUMMARY). Включишь — начнёт агрегировать.
"""

import os
import logging
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

SHEETBEST_URL = os.getenv("SHEETBEST_URL")
ENABLE_SALES_SUMMARY = os.getenv("ENABLE_SALES_SUMMARY", "false").lower() == "true"

def _today() -> str:
    """Текущая дата в формате ДД.ММ.ГГГГ. Вызывается каждый раз, чтобы не протухала после полуночи."""
    return datetime.now().strftime("%d.%m.%Y")


def _date_cell() -> str:
    """Дата для ячейки. Без апострофа — SheetBest хранит его буквально как символ."""
    return _today()


def v(val, default="—"):
    """Значение для ячейки: None и пустое → заглушка, иначе строкой."""
    if val is None:
        return default
    s = str(val).strip()
    return s if s and s.lower() != "none" else default


def _post(tab: str, rows: list):
    """Шлёт строки в одну вкладку одним запросом."""
    if not rows:
        return
    url = f"{SHEETBEST_URL}/tabs/{tab}"
    resp = httpx.post(url, json=rows, timeout=30)
    resp.raise_for_status()


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
    today = _today()
    cnt = 0
    for row in rows:
        # Имя могло быть записано как "Ytka" или "Ytka (отчёт №2)" — сверяем по началу.
        emp = str(row.get("Сотрудник", ""))
        # SheetBest может вернуть дату как с апострофом, так и без — нормализуем оба.
        stored_date = str(row.get("Дата", "")).lstrip("'").strip()
        if stored_date == today and emp.startswith(name):
            cnt += 1
    return cnt


def write_all_sheets(name: str, data: dict, force: bool = False) -> dict:
    """
    Пишет данные во все вкладки.
      {"status": "ok", "report_no": N}  — записано (N — номер отчёта за день)
      {"status": "duplicate"}           — отчёт за сегодня уже есть, нужно подтверждение

    force=True — записать несмотря на уже существующий отчёт (режим "второй отчёт").
    """
    daily = data.get("daily", {})
    clients = data.get("clients", [])
    tasks = data.get("tasks", [])
    operations = data.get("operations", [])

    existing = daily_count_today(name)
    if existing > 0 and not force:
        logger.warning("Отчёт от %s за %s уже есть (%d шт) — нужно подтверждение", name, _today(), existing)
        return {"status": "duplicate"}

    report_no = existing + 1
    # При втором+ отчёте помечаем сотрудника, чтобы строки не выглядели дублем.
    emp_label = name if report_no == 1 else f"{name} (отчёт №{report_no})"
    date_cell = _date_cell()

    # ── Daily Reports — одна строка на отчёт + пустая строка-разделитель перед ней ──
    _post("Daily Reports", [
        {"Дата": "", "Сотрудник": "", "Клиентов за день": "", "Продаж": "",
         "Сумма продаж": "", "КП / счета": "", "Потенциальные сделки": "",
         "Follow-up": "", "Операционные задачи": "", "Итог дня": ""},
        {
            "Дата": date_cell,
            "Сотрудник": emp_label,
            "Клиентов за день": v(daily.get("clients")),
            "Продаж": v(daily.get("sales")),
            "Сумма продаж": v(daily.get("sales_amount")),
            "КП / счета": v(daily.get("kp")),
            "Потенциальные сделки": v(daily.get("potential")),
            "Follow-up": v(daily.get("followup")),
            "Операционные задачи": v(daily.get("tasks")),
            "Итог дня": v(daily.get("summary")),
        },
    ])

    # ── Clients Leads — все клиенты, дедуп внутри отчёта по имени ──
    seen, client_rows = set(), []
    for idx, c in enumerate(clients):
        raw_name = c.get("name")
        # Используем индекс как запасной ключ для анонимных клиентов (name=None),
        # чтобы не схлопнуть двух разных анонимных посетителей в одного.
        key = str(raw_name).strip() if raw_name else f"__anon_{idx}"
        if key in seen:
            continue
        seen.add(key)
        client_rows.append({
            "Дата": date_cell,
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
    if client_rows:
        _post("Clients Leads", client_rows)

    # ── Tasks Follow-up ──
    seen, task_rows = set(), []
    for idx, t in enumerate(tasks):
        raw_task = t.get("task")
        key = str(raw_task).strip() if raw_task else f"__anon_{idx}"
        if key in seen:
            continue
        seen.add(key)
        task_rows.append({
            "Дата создания": date_cell,
            "Задача": key,
            "Связано с": v(t.get("related")),
            "Ответственный": v(t.get("responsible"), name),
            "Срок": v(t.get("deadline")),
            "Приоритет": v(t.get("priority")),
            "Статус": v(t.get("status")),
            "Комментарий": v(t.get("comment")),
        })
    if task_rows:
        _post("Tasks Follow-up", task_rows)

    # ── Issues Operations ──
    seen, op_rows = set(), []
    for idx, op in enumerate(operations):
        raw_desc = op.get("description")
        key = str(raw_desc).strip() if raw_desc else f"__anon_{idx}"
        if key in seen:
            continue
        seen.add(key)
        op_rows.append({
            "Дата": date_cell,
            "Сотрудник": emp_label,
            "Категория": v(op.get("category")),
            "Описание": key,
            "Связано с": v(op.get("related")),
            "Статус": v(op.get("status")),
            "Следующее действие": v(op.get("next_action")),
        })
    if op_rows:
        _post("Issues Operations", op_rows)

    # ── Sales Summary — задел, по умолчанию выключен ──
    if ENABLE_SALES_SUMMARY:
        try:
            update_sales_summary(name, daily)
        except Exception as e:
            logger.warning("Sales Summary не обновлён: %s", e)

    return {"status": "ok", "report_no": report_no}


def update_sales_summary(name: str, daily: dict):
    """ЗАДЕЛ: агрегат продаж по дням. Включается через ENABLE_SALES_SUMMARY=true.
    Сейчас просто дописывает строку-срез; при желании расширишь до недель/месяцев."""
    _post("Sales Summary", [{
        "Дата": _date_cell(),
        "Сотрудник": name,
        "Клиентов": v(daily.get("clients")),
        "Продаж": v(daily.get("sales")),
        "Сумма": v(daily.get("sales_amount")),
        "КП": v(daily.get("kp")),
    }])
