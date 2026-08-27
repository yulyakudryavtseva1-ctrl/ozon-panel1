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
    if not resp.ok:
        # Тело ответа Ozon обычно объясняет, что именно не так с запросом
        # (например message/code) — без этого текста diagnosе почти невозможна,
        # поэтому подмешиваем его в текст исключения вместо голого "400".
        raise RuntimeError(f"{resp.status_code} {resp.reason} at {path}: {resp.text[:500]}")
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

    ИСПРАВЛЕНО (2026-08-27): метрика "returns" в /v1/analytics/data оказалась
    устаревшей — Ozon в реальном ответе вернул {"code":3,"message":"deprecated
    metrics used"}. Перешли на отдельный метод POST /v1/returns/list (по
    документации, найденной в открытых источниках, — сам вызов ещё НЕ
    ПРОВЕРЕН ВЖИВУЮ, потому что до правки метрики он даже не запускался).
    Считаем количество возвратов простым подсчётом строк в ответе — если
    имя поля со списком окажется другим, страховка ниже всё равно найдёт
    первый список в ответе."""
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    payload = {
        "filter": {
            "logistic_return_date": {
                "time_from": f"{date_from.isoformat()}T00:00:00.000Z",
                "time_to": f"{date_to.isoformat()}T23:59:59.999Z",
            }
        },
        "limit": 500,
    }
    try:
        data = ozon_post("/v1/returns/list", payload, headers)
        rows = data.get("returns")
        if rows is None:
            rows = next((v for v in data.values() if isinstance(v, list)), [])
        return {"returns_units": len(rows), "state": "ok"}
    except Exception as exc:
        print(f"Не удалось получить данные по возвратам Ozon (не критично): {exc}", file=sys.stderr)
        return {"state": "error"}


def _extract_expense_amounts(obj):
    """Достаём суммы комиссии/логистики/хранения из одного объекта ответа —
    точные имена полей не подтверждены вживую, поэтому проверяем несколько
    вероятных вариантов написания сразу."""
    commission = float(obj.get("commission_amount") or obj.get("commission") or obj.get("sales_commission") or 0)
    logistics = float(
        obj.get("delivery_amount") or obj.get("logistic_amount") or obj.get("logistics") or obj.get("delivery_charge") or 0
    )
    storage = float(obj.get("storage_amount") or obj.get("storage") or obj.get("storage_fee") or 0)
    return commission, logistics, storage


def fetch_expenses(headers, days=7):
    """Расходы площадки (комиссия, логистика, хранение) за последние N дней.

    ИСПРАВЛЕНО (2026-08-27): в реальном ответе метод потребовал одну дату
    (поле "date" вида ГГГГ-ММ-ДД), а не диапазон "date_from"/"date_to" —
    Ozon вернул ошибку валидации именно об этом. Поэтому теперь запрашиваем
    по одному дню за раз и складываем суммы за days дней. Точные имена полей
    с суммами внутри ответа за один день по-прежнему НЕ ПРОВЕРЕНЫ ВЖИВУЮ —
    если после этой правки числа останутся нулевыми при "state": "ok",
    пришли текст ответа из лога Actions (первая строка stderr ниже), поправим
    имена полей."""
    date_to = datetime.now(MSK).date()
    commission = logistics = storage = 0.0
    ok_days = 0
    last_error = None
    for i in range(days):
        d = date_to - timedelta(days=i)
        try:
            data = ozon_post("/v1/finance/accrual/by-day", {"date": d.isoformat()}, headers)
        except Exception as exc:
            last_error = exc
            continue
        result = data.get("result", data)
        if isinstance(result, list):
            candidates = result
        elif isinstance(result, dict):
            nested_list = next((v for v in result.values() if isinstance(v, list)), None)
            candidates = nested_list if nested_list is not None else [result]
        else:
            candidates = []
        for row in candidates:
            if isinstance(row, dict):
                c, l, s = _extract_expense_amounts(row)
                commission += c
                logistics += l
                storage += s
        ok_days += 1
    if ok_days == 0:
        print(f"Не удалось получить расходы площадки Ozon (не критично): {last_error}", file=sys.stderr)
        return {"state": "error"}
    return {"commission": commission, "logistics": logistics, "storage": storage, "state": "ok"}


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
