"""
Забирает данные из Wildberries Statistics API. Только функции получения
данных — рендерит страницу scripts/build_dashboard.py.

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ: в отличие от Ozon-части этого проекта (которая уже
проверена вживую и работает), интеграция с Wildberries написана по открытой
документации и статьям, но НЕ проверена реальным вызовом — у меня нет
доступа к вашему WB-кабинету, чтобы протестировать её заранее. Первый
реальный запуск (после того как вы добавите WB_API_KEY) покажет, что
именно нужно поправить — почти наверняка это будет мелочь вроде имени поля
в ответе. Пришлите текст ошибки из лога Actions, и мы поправим точно так
же, как чинили Ozon Performance API.

Базовый домен: https://statistics-api.wildberries.ru
Авторизация: заголовок "Authorization: <токен>" (WB, в отличие от Ozon,
использует токен напрямую, без префикса "Bearer" — по открытым источникам).

Важно: у Statistics API официально заявлен лимит примерно 1 запрос в минуту
на метод. Чтобы не упираться в него, весь скрипт делает всего 3 запроса за
прогон (одна выгрузка продаж за 14 дней, одна — остатков, одна — отчёта по
расходам), а не по отдельному запросу на каждую цифру.
"""

import os
import sys
from datetime import datetime, timedelta

import requests

from common import MSK

STATISTICS_BASE_URL = "https://statistics-api.wildberries.ru"


def wb_headers():
    token = os.environ.get("WB_API_KEY")
    if not token:
        raise RuntimeError(
            "Не задан WB_API_KEY — добавь его в Settings > Secrets and variables > "
            "Actions репозитория (Личный кабинет WB → Профиль → Доступ к API)."
        )
    return {"Authorization": token}


def wb_get(path, params, headers):
    resp = requests.get(f"{STATISTICS_BASE_URL}{path}", headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_sales_records(headers, date_from):
    """GET /api/v1/supplier/sales — события продаж и возвратов начиная с
    date_from (WB отдаёт всё, что изменилось с этой даты, дальше фильтруем
    по датам сами). saleID начинается на 'S' — продажа, на 'R' — возврат
    (соглашение по открытым источникам, НЕ ПРОВЕРЕНО ВЖИВУЮ)."""
    params = {"dateFrom": date_from.isoformat(), "flag": 0}
    data = wb_get("/api/v1/supplier/sales", params, headers)
    return data if isinstance(data, list) else []


def _is_return(row):
    return str(row.get("saleID", "")).upper().startswith("R")


def _record_date(row):
    return (row.get("date") or "")[:10]


def build_daily(records, days=7):
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    by_day = {}
    for row in records:
        d = _record_date(row)
        if not d or not (date_from.isoformat() <= d <= date_to.isoformat()):
            continue
        entry = by_day.setdefault(d, {"orders": 0, "revenue": 0.0})
        if not _is_return(row):
            entry["orders"] += 1
            entry["revenue"] += float(row.get("forPay") or 0)

    days_list = []
    for i in range(days):
        d = date_from + timedelta(days=i)
        key = d.isoformat()
        entry = by_day.get(key, {"orders": 0, "revenue": 0})
        days_list.append({"date": key, "orders": entry["orders"], "revenue": entry["revenue"]})
    return days_list


def build_sku_units(records, days):
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    units = {}
    for row in records:
        d = _record_date(row)
        if not d or not (date_from.isoformat() <= d <= date_to.isoformat()) or _is_return(row):
            continue
        nm_id = str(row.get("nmId") or "")
        if nm_id:
            units[nm_id] = units.get(nm_id, 0) + 1
    return units


def build_returns(records, days=7):
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    total = 0
    for row in records:
        d = _record_date(row)
        if d and (date_from.isoformat() <= d <= date_to.isoformat()) and _is_return(row):
            total += 1
    return {"returns_units": total, "state": "ok"}


def fetch_stocks(headers):
    """GET /api/v1/supplier/stocks — текущие остатки по складам, суммируем
    по артикулу (nmId). Параметр dateFrom обязателен у метода, но не влияет
    на "свежесть" остатков — это просто фильтр по дате последнего изменения."""
    params = {"dateFrom": "2020-01-01"}
    data = wb_get("/api/v1/supplier/stocks", params, headers)
    rows = data if isinstance(data, list) else []
    totals = {}
    names = {}
    for row in rows:
        nm_id = str(row.get("nmId") or "")
        if not nm_id:
            continue
        totals[nm_id] = totals.get(nm_id, 0) + int(row.get("quantity") or 0)
        if nm_id not in names:
            names[nm_id] = row.get("supplierArticle") or row.get("subject") or nm_id
    return [{"sku": nm_id, "name": str(names[nm_id]), "present": qty} for nm_id, qty in totals.items()]


def fetch_expenses(headers, days=7):
    """GET /api/v5/supplier/reportDetailByPeriod — комиссия/логистика/
    хранение за период (детальный финотчёт).

    НЕ ПРОВЕРЕНО ВЖИВУЮ. Поля ppvz_sales_commission / delivery_rub /
    rebill_logistic_cost / storage_fee собраны по открытым источникам."""
    date_to = datetime.now(MSK).date()
    date_from = date_to - timedelta(days=days - 1)
    try:
        params = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
            "limit": 100000,
            "rrdid": 0,
        }
        data = wb_get("/api/v5/supplier/reportDetailByPeriod", params, headers)
        rows = data if isinstance(data, list) else []
        commission = logistics = storage = 0.0
        for row in rows:
            commission += float(row.get("ppvz_sales_commission") or 0)
            logistics += float(row.get("delivery_rub") or 0) + float(row.get("rebill_logistic_cost") or 0)
            storage += float(row.get("storage_fee") or 0)
        return {"commission": commission, "logistics": logistics, "storage": storage, "state": "ok"}
    except Exception as exc:
        print(f"Не удалось получить расходы площадки Wildberries (не критично): {exc}", file=sys.stderr)
        return {"state": "error"}


def fetch_dataset():
    """Собирает весь сырой набор данных по Wildberries. Реклама (WB Advert
    API) пока не подключена — это отдельный, более сложный API, добавим
    следующим шагом, аналогично тому, как добавляли рекламу Ozon вторым
    этапом."""
    headers = wb_headers()
    date_to = datetime.now(MSK).date()
    date_from_14 = date_to - timedelta(days=13)
    records = fetch_sales_records(headers, date_from_14)

    daily = build_daily(records, days=7)
    sku_units_14d = build_sku_units(records, days=14)
    velocity = {sku: u / 14 for sku, u in sku_units_14d.items()}
    sku_orders_7d = build_sku_units(records, days=7)
    returns = build_returns(records, days=7)

    stocks = fetch_stocks(headers)
    expenses = fetch_expenses(headers, days=7)

    return {
        "daily": daily,
        "velocity": velocity,
        "sku_orders_7d": sku_orders_7d,
        "stocks": stocks,
        "ads": None,
        "returns": returns,
        "expenses": expenses,
    }


def demo_dataset():
    """Синтетические данные для python scripts/build_dashboard.py --demo."""
    date_to = datetime.now(MSK).date()
    orders_pattern = [420, 455, 401, 389, 460, 512, 498]
    daily = []
    for i, orders in enumerate(orders_pattern):
        d = date_to - timedelta(days=6 - i)
        daily.append({"date": d.isoformat(), "orders": orders, "revenue": orders * 2140})
    velocity = {"wb-demo-1": 6.1, "wb-demo-2": 1.2, "wb-demo-3": 22.4}
    sku_orders_7d = {"wb-demo-1": 43, "wb-demo-2": 8, "wb-demo-3": 156}
    stocks = [
        {"sku": "wb-demo-1", "name": "БрюкиТрикотСИН", "present": 22},
        {"sku": "wb-demo-2", "name": "Чин2- син Шн", "present": 9},
        {"sku": "wb-demo-3", "name": "TР сине утепл.", "present": 310},
    ]
    returns = {"returns_units": 168, "state": "ok"}
    expenses = {"commission": 154200.0, "logistics": 98700.0, "storage": 21300.0, "state": "ok"}
    return {
        "daily": daily,
        "velocity": velocity,
        "sku_orders_7d": sku_orders_7d,
        "stocks": stocks,
        "ads": None,
        "returns": returns,
        "expenses": expenses,
    }
