"""
Собирает данные из Ozon Seller API и пересобирает index.html.

ВАЖНО: названия полей и путей ниже основаны на публично известной структуре
Ozon Seller API (api-seller.ozon.ru, авторизация через заголовки Client-Id и
Api-Key). API у площадок периодически меняется, поэтому при первом запуске
внимательно посмотри вывод в логе GitHub Actions — если какой-то запрос
вернёт ошибку 4xx, скорее всего изменилось имя поля или путь метода, и его
нужно свериться с актуальной документацией: https://docs.ozon.ru/api/seller/

Скрипт спроектирован так, чтобы НЕ ломать сайт при сбое: если какой-то из
запросов не удался, используются последние успешно сохранённые данные
(data/last_data.json), а на страницу добавляется предупреждение.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from jinja2 import Template

BASE_URL = "https://api-seller.ozon.ru"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
LAST_DATA_PATH = os.path.join(DATA_DIR, "last_data.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html.j2")
OUTPUT_PATH = os.path.join(ROOT, "index.html")

MSK = timezone(timedelta(hours=3))


def ozon_headers():
    client_id = os.environ.get("OZON_CLIENT_ID")
    api_key = os.environ.get("OZON_API_KEY")
    if not client_id or not api_key:
        raise RuntimeError(
            "Не заданы OZON_CLIENT_ID / OZON_API_KEY — добавь их в Settings > "
            "Secrets and variables > Actions репозитория."
        )
    return {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }


def ozon_post(path, payload, headers):
    resp = requests.post(f"{BASE_URL}{path}", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_daily_orders(headers, days=7):
    """POST /v1/analytics/data — заказы и выручка по дням за последние N дней.
    Проверить актуальные названия метрик/полей в документации, если запрос упадёт."""
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "metrics": ["ordered_units", "revenue"],
        "dimension": ["day"],
        "limit": 1000,
    }
    data = ozon_post("/v1/analytics/data", payload, headers)
    rows = data.get("result", {}).get("data", [])

    by_day = {}
    for row in rows:
        dims = row.get("dimensions", [])
        day_str = dims[0].get("id") if dims else None
        metrics = row.get("metrics", [0, 0])
        if day_str:
            by_day[day_str] = {
                "orders": metrics[0] if len(metrics) > 0 else 0,
                "revenue": metrics[1] if len(metrics) > 1 else 0,
            }

    days_list = []
    for i in range(days):
        d = date_from + timedelta(days=i)
        key = d.isoformat()
        entry = by_day.get(key, {"orders": 0, "revenue": 0})
        days_list.append({"date": key, "orders": entry["orders"], "revenue": entry["revenue"]})
    return days_list


def fetch_sku_velocity(headers, days=14):
    """Средние продажи в день по SKU за последние N дней — нужно для оценки
    'на сколько дней хватит остатка'. Проверить путь/поля при первом запуске."""
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "metrics": ["ordered_units"],
        "dimension": ["sku"],
        "limit": 1000,
    }
    try:
        data = ozon_post("/v1/analytics/data", payload, headers)
    except Exception:
        return {}
    rows = data.get("result", {}).get("data", [])
    velocity = {}
    for row in rows:
        dims = row.get("dimensions", [])
        sku = dims[0].get("id") if dims else None
        metrics = row.get("metrics", [0])
        units = metrics[0] if metrics else 0
        if sku:
            velocity[str(sku)] = units / days
    return velocity


def fetch_stocks(headers):
    """POST /v3/product/info/stocks — остатки по товарам."""
    payload = {"filter": {"visibility": "ALL"}, "limit": 1000}
    data = ozon_post("/v3/product/info/stocks", payload, headers)
    items = data.get("items") or data.get("result", {}).get("items", [])
    stocks = []
    for item in items:
        present = 0
        for s in item.get("stocks", []):
            present += s.get("present", 0)
        stocks.append(
            {
                "sku": str(item.get("sku") or item.get("product_id") or ""),
                "name": item.get("offer_id") or str(item.get("product_id", "")),
                "present": present,
            }
        )
    return stocks


def build_dataset():
    headers = ozon_headers()
    daily = fetch_daily_orders(headers, days=7)
    velocity = fetch_sku_velocity(headers, days=14)
    stocks = fetch_stocks(headers)
    return {"daily": daily, "velocity": velocity, "stocks": stocks}


def load_cached():
    if os.path.exists(LAST_DATA_PATH):
        with open(LAST_DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cache(dataset):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LAST_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)


def fmt_money(value):
    try:
        return f"{value:,.0f}".replace(",", " ") + " ₽"
    except Exception:
        return "—"


def fmt_delta(current, previous, is_money=False):
    if previous in (None, 0):
        return "flat", "нет данных за предыдущий день"
    change = (current - previous) / previous * 100
    cls = "up" if change > 0 else ("down" if change < 0 else "flat")
    sign = "+" if change > 0 else ""
    return cls, f"{sign}{change:.0f}% к предыдущему дню"


WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def render(dataset, error_message=None):
    daily = dataset["daily"]
    velocity = dataset.get("velocity", {})
    stocks = dataset.get("stocks", [])

    max_orders = max((d["orders"] for d in daily), default=0) or 1
    daily_view = []
    for d in daily:
        dt = datetime.fromisoformat(d["date"])
        height_pct = max(6, round(d["orders"] / max_orders * 100))
        daily_view.append({"label": WEEKDAYS_RU[dt.weekday()], "orders": d["orders"], "height_pct": height_pct})

    orders_yesterday = daily[-1]["orders"] if daily else 0
    orders_prev = daily[-2]["orders"] if len(daily) > 1 else None
    revenue_yesterday = daily[-1]["revenue"] if daily else 0
    revenue_prev = daily[-2]["revenue"] if len(daily) > 1 else None

    orders_delta_class, orders_delta_text = fmt_delta(orders_yesterday, orders_prev)
    revenue_delta_class, revenue_delta_text = fmt_delta(revenue_yesterday, revenue_prev)

    kpi = {
        "orders_yesterday": orders_yesterday,
        "orders_delta_class": orders_delta_class,
        "orders_delta_text": orders_delta_text,
        "revenue_yesterday": fmt_money(revenue_yesterday),
        "revenue_delta_class": revenue_delta_class,
        "revenue_delta_text": revenue_delta_text,
        "orders_7d": sum(d["orders"] for d in daily),
        "revenue_7d": fmt_money(sum(d["revenue"] for d in daily)) + " за 7 дней",
    }

    stock_rows = []
    for s in stocks:
        v = velocity.get(s["sku"], 0)
        if v <= 0:
            continue
        days_left = s["present"] / v if v else None
        if days_left is None or days_left >= 10:
            continue
        if days_left < 3:
            status_class, status_label = "critical", "риск обнуления"
        elif days_left < 7:
            status_class, status_label = "warning", "проверить поставку"
        else:
            status_class, status_label = "good", "следить"
        stock_rows.append(
            {
                "name": s["name"],
                "stock": s["present"],
                "days_left": round(days_left),
                "status_class": status_class,
                "status_label": status_label,
            }
        )
    stock_rows.sort(key=lambda r: r["days_left"])
    stock_rows = stock_rows[:10]

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    html = template.render(
        generated_at=datetime.now(MSK).strftime("%d.%m.%Y, %H:%M МСК"),
        kpi=kpi,
        daily=daily_view,
        stock_rows=stock_rows,
        stock_risk_count=len(stock_rows),
        stock_risk_days=10,
        error_message=error_message,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def demo_dataset():
    """Синтетические данные — чтобы можно было посмотреть, как выглядит
    страница, ещё до подключения реального ключа Ozon (python scripts/fetch_ozon.py --demo)."""
    date_to = datetime.now(MSK).date()
    orders_pattern = [168, 182, 151, 196, 188, 221, 214]
    daily = []
    for i, orders in enumerate(orders_pattern):
        d = date_to - timedelta(days=6 - i)
        daily.append({"date": d.isoformat(), "orders": orders, "revenue": orders * 1806})
    velocity = {"demo-1": 2.8, "demo-2": 0.4, "demo-3": 17.1}
    stocks = [
        {"sku": "demo-1", "name": "Термос 500мл, синий", "present": 14},
        {"sku": "demo-2", "name": "Чехол силикон прозрачный", "present": 61},
        {"sku": "demo-3", "name": "Набор кистей 12 шт", "present": 240},
    ]
    return {"daily": daily, "velocity": velocity, "stocks": stocks}


def main():
    if "--demo" in sys.argv:
        render(demo_dataset())
        print("Готово: index.html собран из демо-данных.")
        return
    try:
        dataset = build_dataset()
        save_cache(dataset)
        render(dataset)
        print("Готово: index.html обновлён свежими данными.")
    except Exception as exc:  # noqa: BLE001 — сознательно широкий except, см. докстринг модуля
        print(f"Ошибка при получении данных Ozon: {exc}", file=sys.stderr)
        cached = load_cached()
        if cached:
            render(cached, error_message=str(exc))
            print("index.html пересобран из последних сохранённых данных.")
        else:
            print("Кэша нет — index.html не изменён.")
            sys.exit(0)  # не роняем workflow, просто ничего не публикуем в этот раз


if __name__ == "__main__":
    main()
