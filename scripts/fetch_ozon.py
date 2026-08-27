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

Опционально (если заданы секреты TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID) —
после успешной сборки отправляет уведомление в Telegram, но только если
есть критичные/требующие внимания задачи. Если секреты не заданы — просто
молча пропускает этот шаг, сайт всё равно обновится.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from jinja2 import Template

BASE_URL = "https://api-seller.ozon.ru"
PERFORMANCE_BASE_URL = "https://api-performance.ozon.ru"
SITE_URL = "https://yulyakudryavtseva1-ctrl.github.io/ozon-panel1/"
# Если ДРР (расход на рекламу / выручка) выше этого порога — заводим задачу
# на сегодня. Число примерное, подправь под свою норму.
DRR_ALERT_THRESHOLD = 15.0
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
    """POST /v4/product/info/stocks — остатки по товарам.

    2026-08-27: раньше здесь был /v3/product/info/stocks, но Ozon вернул
    404 Not Found на этот путь (метод отключён/заменён) — переключено на v4.
    v4 пагинирует через `cursor` (строка), а не `last_id`, как было в v3.
    Структура ответа (items[].stocks[].present) в v4, по доступным
    источникам, осталась той же, что в v3 — но если снова прилетит 4xx,
    проверь актуальную схему по https://docs.ozon.ru/api/seller/ и поправь
    парсинг ниже."""
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


def performance_token():
    """Bearer-токен для Ozon Performance API (рекламный кабинет) — это
    ОТДЕЛЬНЫЕ ключи от Seller API. Получить: личный кабинет Ozon → Настройки
    → API-ключи → раздел Performance API → Добавить ключ (даст Client ID и
    Client Secret, не путать с Client-Id/Api-Key от Seller API).

    Если секреты OZON_PERFORMANCE_CLIENT_ID / OZON_PERFORMANCE_CLIENT_SECRET
    не заданы — возвращает None, и весь блок с рекламой на сайте остаётся
    "скоро", не отключая остальную страницу."""
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
    """Расход на рекламу за последние N дней.

    НЕ ПРОВЕРЕНО ВЖИВУЮ на момент написания (27.08.2026) — собрано по
    описаниям метода в открытых источниках, а не по официальному
    подтверждённому вызову, в отличие от остальных методов в этом файле.
    Если при первом реальном запуске с ключами Performance API в логе
    Actions будет ошибка 4xx — почти наверняка дело в имени параметра или
    пути ниже, поправь по тексту ошибки (и, если нужно, свериться с
    https://docs.ozon.ru/api/performance/).

    Логика: 1) GET /api/client/campaign — список кампаний;
    2) GET /api/client/statistics/expense/json?campaigns=...&dateFrom=...&dateTo=...
    — построчный расход (поле moneySpent), суммируем."""
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
    Задан, но что-то упало → {"error": "..."} — на сайте покажем аккуратное
    сообщение вместо того, чтобы ронять всю страницу (реклама — необязательный
    блок, в отличие от заказов/остатков)."""
    try:
        token = performance_token()
        if not token:
            return None
        return fetch_ad_spend(token, days=days)
    except Exception as exc:
        print(f"Не удалось получить данные по рекламе (не критично, остальная страница не пострадает): {exc}", file=sys.stderr)
        return {"error": str(exc)}


def build_dataset():
    headers = ozon_headers()
    daily = fetch_daily_orders(headers, days=7)
    velocity = fetch_sku_velocity(headers, days=14)
    stocks = fetch_stocks(headers)
    ads = fetch_ads(days=7)
    return {"daily": daily, "velocity": velocity, "stocks": stocks, "ads": ads}


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

# Задачи ниже какого статуса считаем "требующими внимания" для Telegram-уведомления.
ALERT_SEVERITIES = {"critical", "warning"}


def build_tasks(stock_rows, orders_yesterday, orders_prev, drr_pct=None):
    """Превращает сырые цифры в короткий список конкретных дел на сегодня.
    Источники: остатки, резкий провал заказов, ДРР (если подключена реклама).
    Когда подключим отзывы — добавь сюда ещё правил, ничего в шаблоне менять
    не надо."""
    tasks = []

    for r in stock_rows:
        if r["status_class"] == "critical":
            tasks.append(
                {
                    "severity": "critical",
                    "text": f"Пополнить «{r['name']}» — риск обнуления через {r['days_left']} дн. (осталось {r['stock']} шт).",
                }
            )
        elif r["status_class"] == "warning":
            tasks.append(
                {
                    "severity": "warning",
                    "text": f"Проверить поставку «{r['name']}» — хватит на {r['days_left']} дн.",
                }
            )

    if orders_prev not in (None, 0):
        change = (orders_yesterday - orders_prev) / orders_prev * 100
        if change <= -15:
            tasks.append(
                {
                    "severity": "warning",
                    "text": f"Заказы упали на {abs(round(change))}% ко вчера — проверить цену, остатки, рекламу и позиции конкурентов.",
                }
            )

    if drr_pct is not None and drr_pct > DRR_ALERT_THRESHOLD:
        tasks.append(
            {
                "severity": "warning",
                "text": f"ДРР выше нормы: {drr_pct:.0f}% — проверить ставки и эффективность кампаний.",
            }
        )

    order = {"critical": 0, "warning": 1, "good": 2}
    tasks.sort(key=lambda t: order.get(t["severity"], 9))

    if not tasks:
        tasks.append(
            {
                "severity": "good",
                "text": "Критичных задач нет — можно заняться точками роста (например, скилл growth-opportunities-scan).",
            }
        )
    return tasks


def send_telegram_alert(tasks):
    """Шлёт уведомление в Telegram, только если есть критичные/warning задачи
    и в репозитории настроены секреты TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
    Если секретов нет — тихо ничего не делает, сайт при этом всё равно
    обновляется как обычно."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    urgent = [t for t in tasks if t["severity"] in ALERT_SEVERITIES]
    if not urgent:
        return

    lines = ["⚠️ Панель Ozon — есть на что обратить внимание сегодня:", ""]
    for t in urgent:
        prefix = "🔴" if t["severity"] == "critical" else "🟡"
        lines.append(f"{prefix} {t['text']}")
    lines.append("")
    lines.append(SITE_URL)
    text = "\n".join(lines)

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

    ads = dataset.get("ads")
    ads_view = None
    drr_pct = None
    if ads and ads.get("error"):
        ads_view = {"state": "error"}
    elif ads and not ads.get("has_campaigns", True):
        ads_view = {"state": "no_campaigns"}
    elif ads:
        revenue_7d_total = sum(d["revenue"] for d in daily)
        if revenue_7d_total:
            drr_pct = ads["spend"] / revenue_7d_total * 100
        ads_view = {
            "state": "ok",
            "spend": fmt_money(ads["spend"]),
            "drr_pct": round(drr_pct, 1) if drr_pct is not None else None,
        }
    # ads is None → ads_view остаётся None → шаблон покажет "скоро" (ключи не заданы)

    tasks = build_tasks(stock_rows, orders_yesterday, orders_prev, drr_pct=drr_pct)

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    html = template.render(
        generated_at=datetime.now(MSK).strftime("%d.%m.%Y, %H:%M МСК"),
        kpi=kpi,
        daily=daily_view,
        stock_rows=stock_rows,
        stock_risk_count=len(stock_rows),
        stock_risk_days=10,
        tasks=tasks,
        ads=ads_view,
        error_message=error_message,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    return tasks


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
    ads = {"spend": 21400.0, "has_campaigns": True}
    return {"daily": daily, "velocity": velocity, "stocks": stocks, "ads": ads}


def main():
    if "--demo" in sys.argv:
        render(demo_dataset())
        print("Готово: index.html собран из демо-данных.")
        return
    try:
        dataset = build_dataset()
        save_cache(dataset)
        tasks = render(dataset)
        print("Готово: index.html обновлён свежими данными.")
        send_telegram_alert(tasks)
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
