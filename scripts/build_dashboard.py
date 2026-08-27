"""
Главный сценарий: забирает данные с Ozon и Wildberries (каждый — в своём
файле, fetch_ozon.py / fetch_wb.py), считает единые метрики по каждому
(включая чистую прибыль по себестоимости из data/costs.json) и собирает
index.html с обоими кабинетами на одной странице.

Спроектировано так, чтобы сбой одного маркетплейса не портил другой: если,
например, у Wildberries истёк ключ, Ozon-часть страницы всё равно обновится
как обычно, а по WB покажутся последние сохранённые данные с пометкой об
ошибке (или мягкая карточка "ещё не подключено", если ключ вообще не задан).
"""

import json
import os
import sys
from datetime import datetime

from jinja2 import Template

import fetch_ozon
import fetch_wb
from common import (
    DATA_DIR,
    LAST_DATA_PATH,
    MSK,
    OUTPUT_PATH,
    TEMPLATE_PATH,
    WEEKDAYS_RU,
    build_marketplace_tasks,
    fmt_delta,
    fmt_money,
    load_costs,
    send_telegram_alert,
    sort_tasks,
)

MARKETPLACES = [
    {"key": "ozon", "name": "Ozon", "module": fetch_ozon, "secrets": ["OZON_CLIENT_ID", "OZON_API_KEY"]},
    {"key": "wb", "name": "Wildberries", "module": fetch_wb, "secrets": ["WB_API_KEY"]},
]


def load_cached():
    if os.path.exists(LAST_DATA_PATH):
        try:
            with open(LAST_DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(fresh_by_key):
    """fresh_by_key: {"ozon": dataset|None, "wb": dataset|None} — пишем
    только те кабинеты, для которых в этот раз реально пришли свежие
    данные; остальное в кэше остаётся как было."""
    os.makedirs(DATA_DIR, exist_ok=True)
    cache = load_cached()
    for key, dataset in fresh_by_key.items():
        if dataset is not None:
            cache[key] = dataset
    with open(LAST_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_marketplace_data(module, secret_names, cached_entry):
    """Возвращает (dataset, status, error):
    status = 'ok' — свежие данные;
    status = 'cached' — свежий запрос не удался, показываем последний кэш;
    status = 'not_configured' — секреты ещё не заданы, кабинет просто не подключён;
    status = 'unavailable' — запрос не удался и кэша тоже нет."""
    if not all(os.environ.get(name) for name in secret_names):
        return None, "not_configured", None
    try:
        return module.fetch_dataset(), "ok", None
    except Exception as exc:
        print(f"Ошибка при получении данных: {exc}", file=sys.stderr)
        if cached_entry:
            return cached_entry, "cached", str(exc)
        return None, "unavailable", str(exc)


def process_marketplace(mp_key, mp_name, dataset, costs_for_mp):
    daily = dataset["daily"]
    velocity = dataset.get("velocity", {})
    stocks = dataset.get("stocks", [])
    sku_orders_7d = dataset.get("sku_orders_7d", {})

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
    revenue_7d = sum(d["revenue"] for d in daily)
    orders_7d = sum(d["orders"] for d in daily)

    orders_delta_class, orders_delta_text = fmt_delta(orders_yesterday, orders_prev)
    revenue_delta_class, revenue_delta_text = fmt_delta(revenue_yesterday, revenue_prev)

    kpi = {
        "orders_yesterday": orders_yesterday,
        "orders_delta_class": orders_delta_class,
        "orders_delta_text": orders_delta_text,
        "revenue_yesterday": fmt_money(revenue_yesterday),
        "revenue_delta_class": revenue_delta_class,
        "revenue_delta_text": revenue_delta_text,
        "orders_7d": orders_7d,
        "revenue_7d": fmt_money(revenue_7d) + " за 7 дней",
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

    # Реклама / ДРР
    ads = dataset.get("ads")
    ads_view = None
    drr_pct = None
    ad_spend = 0.0
    if ads and ads.get("error"):
        ads_view = {"state": "error"}
    elif ads and not ads.get("has_campaigns", True):
        ads_view = {"state": "no_campaigns"}
    elif ads:
        ad_spend = ads.get("spend", 0.0)
        if revenue_7d:
            drr_pct = ad_spend / revenue_7d * 100
        ads_view = {
            "state": "ok",
            "spend": fmt_money(ad_spend),
            "drr_pct": round(drr_pct, 1) if drr_pct is not None else None,
        }

    # Возвраты / процент выкупа
    returns = dataset.get("returns")
    buyout_view = None
    buyout_pct = None
    if returns and returns.get("state") == "ok":
        returns_units = returns.get("returns_units", 0)
        denom = orders_7d + returns_units
        if denom:
            buyout_pct = orders_7d / denom * 100
        buyout_view = {
            "state": "ok",
            "pct": round(buyout_pct, 1) if buyout_pct is not None else None,
            "returns_units": returns_units,
        }
    elif returns and returns.get("state") == "error":
        buyout_view = {"state": "error"}

    # Расходы площадки
    expenses = dataset.get("expenses")
    expenses_view = None
    expenses_total = 0.0
    if expenses and expenses.get("state") == "ok":
        expenses_total = expenses.get("commission", 0) + expenses.get("logistics", 0) + expenses.get("storage", 0)
        expenses_view = {
            "state": "ok",
            "commission": fmt_money(expenses.get("commission", 0)),
            "logistics": fmt_money(expenses.get("logistics", 0)),
            "storage": fmt_money(expenses.get("storage", 0)),
            "total": fmt_money(expenses_total),
        }
    elif expenses and expenses.get("state") == "error":
        expenses_view = {"state": "error"}

    # Себестоимость проданного за 7 дней → чистая прибыль
    cost_of_goods = 0.0
    unknown_units = 0
    for sku, units in sku_orders_7d.items():
        cost = costs_for_mp.get(str(sku))
        if cost is None:
            unknown_units += units
            continue
        cost_of_goods += cost * units

    net_profit_raw = None
    net_profit_view = None
    if costs_for_mp:
        net_profit_raw = revenue_7d - cost_of_goods - expenses_total - ad_spend
        net_profit_view = {
            "state": "partial" if unknown_units > 0 else "ok",
            "value": fmt_money(net_profit_raw),
            "unknown_units": unknown_units,
        }

    tasks = build_marketplace_tasks(
        mp_name, stock_rows, orders_yesterday, orders_prev, drr_pct=drr_pct, buyout_pct=buyout_pct
    )

    return {
        "key": mp_key,
        "name": mp_name,
        "kpi": kpi,
        "daily": daily_view,
        "stock_rows": stock_rows,
        "stock_risk_count": len(stock_rows),
        "stock_risk_days": 10,
        "ads": ads_view,
        "buyout": buyout_view,
        "expenses": expenses_view,
        "net_profit": net_profit_view,
        "revenue_7d_raw": revenue_7d,
        "net_profit_raw": net_profit_raw,
        "tasks": tasks,
    }


def render(ozon_info, wb_info):
    """ozon_info / wb_info — кортежи (dataset_или_None, status, error_или_None)
    из get_marketplace_data. Возвращает объединённый список задач (для
    Telegram-уведомления)."""
    costs = load_costs()
    specs = [("ozon", "Ozon", ozon_info), ("wb", "Wildberries", wb_info)]

    mp_views = []
    all_tasks = []
    for key, name, (dataset, status, error) in specs:
        if dataset is None:
            mp_views.append({"key": key, "name": name, "status": status, "error": error, "unavailable": True})
            continue
        view = process_marketplace(key, name, dataset, costs.get(key, {}))
        view["status"] = status
        view["error"] = error
        view["unavailable"] = False
        mp_views.append(view)
        all_tasks.extend(view["tasks"])

    all_tasks = sort_tasks(all_tasks)

    connected_views = [v for v in mp_views if not v["unavailable"]]
    total_revenue_7d = sum(v.get("revenue_7d_raw", 0) for v in connected_views)
    net_profit_values = [v.get("net_profit_raw") for v in connected_views if v.get("net_profit_raw") is not None]
    combined_net_profit = sum(net_profit_values) if net_profit_values else None

    critical_count = sum(1 for t in all_tasks if t["severity"] == "critical")
    warning_count = sum(1 for t in all_tasks if t["severity"] == "warning")

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    html = template.render(
        generated_at=datetime.now(MSK).strftime("%d.%m.%Y, %H:%M МСК"),
        marketplaces=mp_views,
        tasks=all_tasks,
        critical_count=critical_count,
        warning_count=warning_count,
        connected_count=len(connected_views),
        total_revenue_7d=fmt_money(total_revenue_7d) if connected_views else None,
        combined_net_profit=fmt_money(combined_net_profit) if combined_net_profit is not None else None,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    return all_tasks


def main():
    if "--demo" in sys.argv:
        render(
            (fetch_ozon.demo_dataset(), "ok", None),
            (fetch_wb.demo_dataset(), "ok", None),
        )
        print("Готово: index.html собран из демо-данных (Ozon + Wildberries).")
        return

    cached = load_cached()

    ozon_info = get_marketplace_data(fetch_ozon, ["OZON_CLIENT_ID", "OZON_API_KEY"], cached.get("ozon"))
    wb_info = get_marketplace_data(fetch_wb, ["WB_API_KEY"], cached.get("wb"))

    save_cache(
        {
            "ozon": ozon_info[0] if ozon_info[1] == "ok" else None,
            "wb": wb_info[0] if wb_info[1] == "ok" else None,
        }
    )

    if ozon_info[0] is None and wb_info[0] is None:
        print("Нет данных ни по одному кабинету (ни свежих, ни кэша) — index.html не изменён.")
        sys.exit(0)

    tasks = render(ozon_info, wb_info)
    print(f"Готово: index.html обновлён (Ozon: {ozon_info[1]}, Wildberries: {wb_info[1]}).")
    send_telegram_alert(tasks)


if __name__ == "__main__":
    main()
