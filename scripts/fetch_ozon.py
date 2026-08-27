"""
Забирает данные из Ozon Seller API + Ozon Performance API. Только функции
получения данных — ничего не рендерит и не пишет index.html сам (это делает
scripts/build_dashboard.py, который объединяет Ozon и Wildberries на одной
странице).

ВАЖНО: названия полей и путей ниже основаны на публично известной структуре
Ozon Seller API (api-seller.ozon.ru, авторизация через заголовки Client-Id и
Api-Key). API у площадок периодически меняется — если какой-то запрос
вернёт ошибку 4xx, смотри лог GitHub Actions и сверяйся с
https://docs.ozon.ru/api/seller/. Части, отмеченные "НЕ ПРОВЕРЕНО ВЖИВУЮ",
собраны по открытым описаниям метода, а не по подтверждённому вызову —
они обёрнуты так, чтобы ошибка в них не ломала остальную страницу.
"""

import os
import sys
from datetime import datetime, timedelta

import requests

from common import MSK

BASE_URL = "https://api-seller.ozon.ru"
PERFORMANCE_BASE_URL = "https://api-performance.ozon.ru"


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
    Проверено вживую, стабильно работает с 2026-08-27."""
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


def fetch_sku_units(headers, days):
    """{sku: продано штук за последние days дней}. Общая основа и для
    "скорости продаж" (велосити, для остатков), и для расчёта себестоимости
    проданного за неделю. Сбоит — тихо возвращает {}, не ломая страницу."""
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
    units = {}
    for row in rows:
        dims = row.get("dimensions", [])
        sku = dims[0].get("id") if dims else None
        metrics = row.get("metrics", [0])
        u = metrics[0] if metrics else 0
        if sku:
            units[str(sku)] = u
    return units


def fetch_sku_velocity(headers, days=14):
    """Средние продажи в день по SKU за последние N дней — для оценки
    'на сколько дней хватит остатка'."""
    units = fetch_sku_units(headers, days)
    return {sku: u / days for sku, u in units.items()}


def fetch_stocks(headers):
    """POST /v4/product/info/stocks — остатки по товарам. Проверено вживую."""
    payload = {"filter": {"visibility": "ALL"}, "cursor": "", "limit": 1000}
    data = ozon_post("/v4/product/info/stocks", payload, headers)
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


def fetch_returns(headers, days=7):
    """Возвраты за последние N дней — нужно для процента выкупа.

    НЕ ПРОВЕРЕНО ВЖИВУЮ: имя метрики "returns" в /v1/analytics/data собрано
    по открытым источникам. Если в логе Actions будет ошибка или пустой
    ответ — пришли текст ошибки, поправим имя метрики/метод."""
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    payload = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "metrics": ["returns"],
        "dimension": ["day"],
        "limit": 1000,
    }
    try:
        data = ozon_post("/v1/analytics/data", payload, headers)
        rows = data.get("result", {}).get("data", [])
        total = 0
        for row in rows:
            metrics = row.get("metrics", [0])
            total += metrics[0] if metrics else 0
        return {"returns_units": total, "state": "ok"}
    except Exception as exc:
        print(f"Не удалось получить данные по возвратам Ozon (не критично): {exc}", file=sys.stderr)
        return {"state": "error"}


def fetch_expenses(headers, days=7):
    """Расходы площадки (комиссия, логистика, хранение) за последние N дней.

    НЕ ПРОВЕРЕНО ВЖИВУЮ (2026-08-27): Ozon отключил старый метод
    /v3/finance/transaction/list 6 июля 2026 и заменил его тремя новыми:
    /v1/finance/accrual/postings, /v1/finance/accrual/types,
    /v1/finance/accrual/by-day. Используем by-day (готовые суммы по дням) —
    но точные поля ответа здесь наименее проверены из всего файла. Если в
    логе Actions будет ошибка — пришли текст, поправим по факту, как чинили
    остатки и рекламу раньше."""
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    payload = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    try:
        data = ozon_post("/v1/finance/accrual/by-day", payload, headers)
        result = data.get("result", data)
        rows = result if isinstance(result, list) else result.get("rows", result.get("days", []))
        commission = logistics = storage = 0.0
        for row in rows:
            commission += float(row.get("commission_amount") or row.get("commission") or 0)
            logistics += float(row.get("delivery_amount") or row.get("logistic_amount") or row.get("logistics") or 0)
            storage += float(row.get("storage_amount") or row.get("storage") or 0)
        return {"commission": commission, "logistics": logistics, "storage": storage, "state": "ok"}
    except Exception as exc:
        print(f"Не удалось получить расходы площадки Ozon (не критично): {exc}", file=sys.stderr)
        return {"state": "error"}


def performance_token():
    """Bearer-токен для Ozon Performance API (рекламный кабинет) — это
    ОТДЕЛЬНЫЕ ключи от Seller API. Получить: личный кабинет Ozon → Настройки
    → API-ключи → раздел Performance API → Добавить ключ.

    Если секреты OZON_PERFORMANCE_CLIENT_ID / OZON_PERFORMANCE_CLIENT_SECRET
    не заданы — возвращает None, и блок с рекламой остаётся "скоро"."""
    client_id = os.environ.get("OZON_PERFORMANCE_CLIENT_ID")
    client_secret = os.environ.get("OZON_PERFORMANCE_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    resp = requests.post(
        f"{PERFORMANCE_BASE_URL}/api/client/token",
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Ozon Performance API не вернул access_token")
    return token


def fetch_ad_spend(token, days=7):
    """Расход на рекламу за последние N дней. Проверено вживую с
    2026-08-27 (домен api-performance.ozon.ru, не старый performance.ozon.ru)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    camp_resp = requests.get(f"{PERFORMANCE_BASE_URL}/api/client/campaign", headers=headers, timeout=30)
    camp_resp.raise_for_status()
    campaigns = camp_resp.json().get("list", [])
    campaign_ids = [str(c.get("id")) for c in campaigns if c.get("id")]
    if not campaign_ids:
        return {"spend": 0.0, "has_campaigns": False}

    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    params = {
        "campaigns": campaign_ids,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
    }
    exp_resp = requests.get(
        f"{PERFORMANCE_BASE_URL}/api/client/statistics/expense/json",
        headers=headers,
        params=params,
        timeout=30,
    )
    exp_resp.raise_for_status()
    data = exp_resp.json()
    rows = data if isinstance(data, list) else (data.get("rows") or data.get("list") or [])
    spend = 0.0
    for row in rows:
        try:
            spend += float(str(row.get("moneySpent", 0)).replace(",", "."))
        except (TypeError, ValueError):
            continue
    return {"spend": spend, "has_campaigns": True}


def fetch_ads(days=7):
    """Обёртка над блоком рекламы: не задан ключ → None (тихо).
    Задан, но что-то упало → {"error": "..."}."""
    try:
        token = performance_token()
        if not token:
            return None
        return fetch_ad_spend(token, days=days)
    except Exception as exc:
        print(f"Не удалось получить данные по рекламе Ozon (не критично): {exc}", file=sys.stderr)
        return {"error": str(exc)}


def fetch_dataset():
    """Собирает весь сырой набор данных по Ozon. Каждый необязательный блок
    (возвраты, расходы, реклама) сам ловит свои ошибки и не роняет остальное —
    падает только если не задан сам ключ Ozon Seller API (ozon_headers)."""
    headers = ozon_headers()
    daily = fetch_daily_orders(headers, days=7)
    velocity = fetch_sku_velocity(headers, days=14)
    sku_orders_7d = fetch_sku_units(headers, days=7)
    stocks = fetch_stocks(headers)
    ads = fetch_ads(days=7)
    returns = fetch_returns(headers, days=7)
    expenses = fetch_expenses(headers, days=7)
    return {
        "daily": daily,
        "velocity": velocity,
        "sku_orders_7d": sku_orders_7d,
        "stocks": stocks,
        "ads": ads,
        "returns": returns,
        "expenses": expenses,
    }


def demo_dataset():
    """Синтетические данные для python scripts/build_dashboard.py --demo."""
    date_to = datetime.now(MSK).date()
    orders_pattern = [168, 182, 151, 196, 188, 221, 214]
    daily = []
    for i, orders in enumerate(orders_pattern):
        d = date_to - timedelta(days=6 - i)
        daily.append({"date": d.isoformat(), "orders": orders, "revenue": orders * 1806})
    velocity = {"demo-1": 2.8, "demo-2": 0.4, "demo-3": 17.1}
    sku_orders_7d = {"demo-1": 20, "demo-2": 3, "demo-3": 120}
    stocks = [
        {"sku": "demo-1", "name": "Термос 500мл, синий", "present": 14},
        {"sku": "demo-2", "name": "Чехол силикон прозрачный", "present": 61},
        {"sku": "demo-3", "name": "Набор кистей 12 шт", "present": 240},
    ]
    ads = {"spend": 21400.0, "has_campaigns": True}
    returns = {"returns_units": 42, "state": "ok"}
    expenses = {"commission": 61200.0, "logistics": 38900.0, "storage": 4100.0, "state": "ok"}
    return {
        "daily": daily,
        "velocity": velocity,
        "sku_orders_7d": sku_orders_7d,
        "stocks": stocks,
        "ads": ads,
        "returns": returns,
        "expenses": expenses,
    }
