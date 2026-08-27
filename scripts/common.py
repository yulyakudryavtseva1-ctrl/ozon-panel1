"""
Общие вещи для обоих маркетплейсов (Ozon и Wildberries): пути, форматирование,
себестоимость из data/costs.json, сборка списка задач, отправка в Telegram.

Ничего здесь не обращается к API площадок — это делают fetch_ozon.py и
fetch_wb.py, каждый в своём файле.
"""

import json
import os
import sys
from datetime import timedelta, timezone

MSK = timezone(timedelta(hours=3))
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Пороги для авто-задач "на сегодня" — примерные, подправь под свою норму.
DRR_ALERT_THRESHOLD = 15.0
BUYOUT_ALERT_THRESHOLD = 60.0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
COSTS_PATH = os.path.join(DATA_DIR, "costs.json")
LAST_DATA_PATH = os.path.join(DATA_DIR, "last_data.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html.j2")
OUTPUT_PATH = os.path.join(ROOT, "index.html")
SITE_URL = "https://yulyakudryavtseva1-ctrl.github.io/ozon-panel1/"

ALERT_SEVERITIES = {"critical", "warning"}


def fmt_money(value):
    try:
        sign = "-" if value < 0 else ""
        return f"{sign}{abs(value):,.0f}".replace(",", " ") + " ₽"
    except Exception:
        return "—"


def fmt_delta(current, previous):
    if previous in (None, 0):
        return "flat", "нет данных за предыдущий день"
    change = (current - previous) / previous * 100
    cls = "up" if change > 0 else ("down" if change < 0 else "flat")
    sign = "+" if change > 0 else ""
    return cls, f"{sign}{change:.0f}% к предыдущему дню"


def load_costs():
    """Себестоимость товаров Юля ведёт вручную в data/costs.json:

        {"ozon": {"<sku или offer_id>": 450}, "wb": {"<nm_id>": 450}}

    Файла нет, он пустой или битый — просто считаем, что себестоимости нет
    ни у одного товара (чистая прибыль будет показана как "неполная", а не
    сломает страницу)."""
    if not os.path.exists(COSTS_PATH):
        return {"ozon": {}, "wb": {}}
    try:
        with open(COSTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "ozon": {str(k): v for k, v in (data.get("ozon") or {}).items()},
            "wb": {str(k): v for k, v in (data.get("wb") or {}).items()},
        }
    except Exception as exc:
        print(f"Не удалось прочитать data/costs.json ({exc}) — считаем, что себестоимость не задана.", file=sys.stderr)
        return {"ozon": {}, "wb": {}}


def build_marketplace_tasks(mp_name, stock_rows, orders_yesterday, orders_prev, drr_pct=None, buyout_pct=None):
    """Короткий список конкретных дел по одному маркетплейсу. Каждая задача
    помечена префиксом [Ozon]/[Wildberries], чтобы при объединении с другим
    маркетплейсом было понятно, к какому кабинету она относится."""
    tasks = []

    for r in stock_rows:
        if r["status_class"] == "critical":
            tasks.append(
                {
                    "severity": "critical",
                    "text": f"[{mp_name}] Пополнить «{r['name']}» — риск обнуления через {r['days_left']} дн. (осталось {r['stock']} шт).",
                }
            )
        elif r["status_class"] == "warning":
            tasks.append(
                {
                    "severity": "warning",
                    "text": f"[{mp_name}] Проверить поставку «{r['name']}» — хватит на {r['days_left']} дн.",
                }
            )

    if orders_prev not in (None, 0):
        change = (orders_yesterday - orders_prev) / orders_prev * 100
        if change <= -15:
            tasks.append(
                {
                    "severity": "warning",
                    "text": f"[{mp_name}] Заказы упали на {abs(round(change))}% ко вчера — проверить цену, остатки, рекламу и позиции конкурентов.",
                }
            )

    if drr_pct is not None and drr_pct > DRR_ALERT_THRESHOLD:
        tasks.append(
            {
                "severity": "warning",
                "text": f"[{mp_name}] ДРР выше нормы: {drr_pct:.0f}% — проверить ставки и эффективность кампаний.",
            }
        )

    if buyout_pct is not None and buyout_pct < BUYOUT_ALERT_THRESHOLD:
        tasks.append(
            {
                "severity": "warning",
                "text": f"[{mp_name}] Низкий процент выкупа: {buyout_pct:.0f}% — проверить качество карточки, размеры, возможный брак.",
            }
        )

    return tasks


def sort_tasks(tasks):
    order = {"critical": 0, "warning": 1, "good": 2}
    tasks = sorted(tasks, key=lambda t: order.get(t["severity"], 9))
    if not tasks:
        tasks = [
            {
                "severity": "good",
                "text": "Критичных задач нет — можно заняться точками роста (например, скилл growth-opportunities-scan).",
            }
        ]
    return tasks


def send_telegram_alert(tasks):
    """Шлёт уведомление в Telegram, только если есть критичные/warning задачи
    и заданы секреты TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Иначе — тихо
    ничего не делает, сайт при этом обновляется как обычно."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    urgent = [t for t in tasks if t["severity"] in ALERT_SEVERITIES]
    if not urgent:
        return

    lines = ["⚠️ Панель кабинетов — есть на что обратить внимание сегодня:", ""]
    for t in urgent:
        prefix = "🔴" if t["severity"] == "critical" else "🟡"
        lines.append(f"{prefix} {t['text']}")
    lines.append("")
    lines.append(SITE_URL)
    text = "\n".join(lines)

    import requests

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"Telegram вернул ошибку {resp.status_code}: {resp.text}", file=sys.stderr)
    except Exception as exc:
        print(f"Не удалось отправить уведомление в Telegram: {exc}", file=sys.stderr)
