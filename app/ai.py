"""
"Ask AI" (question -> SQL -> answer) and the daily narrative, using Google Gemini.

Safety: the model only proposes SQL. The app accepts a single SELECT / WITH statement,
rejects anything that writes, runs it as a SELECT-only database user (or a read-only
session), with a time limit and a row limit, and shows the SQL next to the answer.
"""
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pymysql
import requests

from . import db

IST = timezone(timedelta(hours=5, minutes=30))
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|call|load|replace|rename|"
                       r"handler|lock|unlock|set|sleep|benchmark|outfile|dumpfile|load_file|shutdown|kill)\b", re.I)

SCHEMA_NOTES = """
MySQL 8 database `food_delivery` of a Swiggy/Zomato-style app in India. All times are India time (IST).
Tables (key columns):
- cities(city_id, city_name, state)                                     49 cities in 32 states/UTs
- zones(zone_id, city_id, zone_name, latitude, longitude)              localities
- customers(customer_id, full_name, email, phone, zone_id, signup_date, is_active)
- restaurants(restaurant_id, restaurant_name, zone_id, cuisine_type, is_veg_only, avg_rating, total_ratings,
              opening_time, closing_time, onboarded_on, is_active)
- menu_items(item_id, restaurant_id, item_name, category, price, is_veg, stock_qty, is_available)
- delivery_partners(partner_id, full_name, phone, zone_id, vehicle_type ENUM('Bike','Scooter','Bicycle','EV'),
                    current_lat, current_lng, is_available, joined_on)
- coupons(coupon_id, coupon_code, discount_type ENUM('PERCENT','FLAT'), discount_value, max_discount,
          min_order_value, valid_from, valid_to, is_active)
- orders(order_id, customer_id, restaurant_id, partner_id, coupon_id, order_time, status
         ENUM('PLACED','CONFIRMED','PREPARING','PICKED_UP','DELIVERED','CANCELLED'), item_total, discount_amount,
         delivery_fee, final_amount, promised_minutes, delivered_time)
- order_items(order_item_id, order_id, item_id, quantity, unit_price)
- payments(payment_id, order_id, payment_method ENUM('UPI','CARD','WALLET','NETBANKING','COD'), amount,
           payment_status ENUM('SUCCESS','FAILED','REFUNDED','PENDING'), paid_at)
- ratings(rating_id, order_id, customer_id, restaurant_id, food_rating 1-5, delivery_rating 1-5, review_text, rated_at)
- order_status_log(log_id, order_id, old_status, new_status, changed_at)
Views: vw_order_details (one row per order with state, city_name, restaurant_zone, restaurant_name, cuisine_type,
       customer, partner, vehicle, coupon, payment, delivery minutes, is_late ...), vw_daily_city_revenue(order_date,
       state, city_name, total_orders, delivered_orders, cancel_rate_pct, revenue, avg_order_value, discount_given),
       vw_monthly_kpis, vw_restaurant_performance, vw_partner_performance, vw_customer_360, vw_coupon_performance.
Joins: orders -> restaurants -> zones -> cities gives the city/state of an order.
Definitions: revenue = SUM(final_amount) of DELIVERED orders. Late = DELIVERED and
TIMESTAMPDIFF(SECOND, order_time, delivered_time)/60 > promised_minutes. "Today" = DATE(order_time) = CURDATE().
Money is Indian rupees (Rs).
"""

_cache, _calls = {}, deque()
_ip_calls = {}
_lock = threading.Lock()


def enabled():
    return bool(os.environ.get("GEMINI_API_KEY"))


def _gemini(prompt, json_out=True, temperature=0.1):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, **({"responseMimeType": "application/json"} if json_out else {})}}
    r = requests.post(URL.format(MODEL), json=body, timeout=40,
                      headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"})
    if r.status_code == 429:
        raise RuntimeError("The free AI quota is used up for now. Please try again in a minute.")
    r.raise_for_status()
    js = r.json()
    text = js["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text) if json_out else text.strip()


def _allowed(ip):
    t = time.time()
    with _lock:
        while _calls and t - _calls[0] > 86400:
            _calls.popleft()
        if len(_calls) >= int(os.environ.get("AI_DAILY_LIMIT", "300")):
            return "The AI chat has reached today's free limit. Please come back tomorrow."
        q = _ip_calls.setdefault(ip, deque())
        while q and t - q[0] > 60:
            q.popleft()
        if len(q) >= 5:
            return "Please wait a minute before asking more questions."
        q.append(t)
        _calls.append(t)
    return None


def safe_sql(sql):
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("Only one statement is allowed")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise ValueError("Only SELECT queries are allowed")
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", s)):
        raise ValueError("The query contains a keyword that is not allowed")
    if not re.search(r"(?i)\blimit\s+\d+", s):
        s += " LIMIT 50"
    return s


def run_readonly(sql):
    conn, _reader = db.reader_connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            c.execute("SET SESSION MAX_EXECUTION_TIME = 8000")
            t0 = time.time()
            c.execute(sql)
            rows = c.fetchmany(200)
            cols = [d[0] for d in c.description] if c.description else []
            return cols, rows, round((time.time() - t0) * 1000)
    finally:
        conn.close()


def _jsonable(v):
    if isinstance(v, (datetime,)):
        return v.isoformat(sep=" ")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return float(v) if str(v).replace(".", "", 1).lstrip("-").isdigit() else str(v)


EXAMPLES = {
    "which state has the most orders today?":
        "SELECT ci.state, COUNT(*) AS orders FROM orders o JOIN restaurants r ON r.restaurant_id = o.restaurant_id "
        "JOIN zones z ON z.zone_id = r.zone_id JOIN cities ci ON ci.city_id = z.city_id "
        "WHERE DATE(o.order_time) = CURDATE() GROUP BY ci.state ORDER BY orders DESC LIMIT 10",
    "what are the 5 biggest orders today?":
        "SELECT order_id, city_name, restaurant_name, final_amount, status FROM vw_order_details "
        "WHERE order_date = CURDATE() ORDER BY final_amount DESC LIMIT 5",
    "how many orders are on the way right now?":
        "SELECT status, COUNT(*) AS orders FROM orders WHERE status IN ('PLACED','CONFIRMED','PREPARING','PICKED_UP') "
        "GROUP BY status",
}


def ask(question, ip="?"):
    q = " ".join(question.strip().split())[:300]
    if len(q) < 4:
        return {"error": "Please type a question."}
    key = q.lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    today = datetime.now(IST).strftime("%A %d %B %Y, %H:%M")
    if not enabled():
        if key in EXAMPLES:
            cols, rows, ms = run_readonly(safe_sql(EXAMPLES[key]))
            out = {"answer": "Here is the result (the AI chat is not switched on, so this is a saved example query).",
                   "sql": EXAMPLES[key], "columns": cols, "rows": [[_jsonable(r[c]) for c in cols] for r in rows], "ms": ms}
            return out
        return {"error": "The AI chat is not switched on yet. Try one of the example questions."}
    why = _allowed(ip)
    if why:
        return {"error": why}
    plan = _gemini(SCHEMA_NOTES + f"\nNow it is {today} (IST).\n"
                   "Write ONE MySQL 8 SELECT query that answers the question below. Use only the tables and views above. "
                   "Prefer clear column aliases, round money to whole rupees, and add LIMIT 50 or less. "
                   "If the question is not about this data, return {\"cannot_answer\": \"short reason\"}.\n"
                   "Return JSON: {\"sql\": \"...\", \"title\": \"a 3-6 word title for the result\"}.\n"
                   f"Question: {q}")
    if plan.get("cannot_answer") or not plan.get("sql"):
        return {"error": plan.get("cannot_answer") or "I could not turn that into a query. Try asking about orders, revenue, cities, restaurants, riders or coupons."}
    sql = safe_sql(plan["sql"])
    try:
        cols, rows, ms = run_readonly(sql)
    except Exception as e:  # noqa: BLE001
        _log(q, sql, False, 0)
        return {"error": f"The query did not run: {str(e)[:160]}", "sql": sql}
    table = [[_jsonable(r[c]) for c in cols] for r in rows]
    answer = _gemini(f"Question: {q}\nSQL that was run:\n{sql}\nResult columns: {cols}\nFirst rows (JSON): "
                     f"{json.dumps(table[:30], default=str)}\nRow count: {len(table)}\n"
                     "Answer the question in 2-4 plain sentences for a business user, using the numbers from the result. "
                     "Mention specific order IDs, cities or restaurants from the rows when relevant. Use Indian number "
                     "formatting with the rupee sign. If the result is empty, say so. Do not mention SQL.",
                     json_out=False, temperature=0.2)
    out = {"answer": answer, "title": plan.get("title"), "sql": sql, "columns": cols, "rows": table[:50], "ms": ms}
    _log(q, sql, True, len(table))
    _cache[key] = (time.time(), out)
    return out


def _log(q, sql, ok, n):
    try:
        with db.cursor() as c:
            c.execute("INSERT INTO ai_log (asked_at, question, sql_text, ok, rows_returned) VALUES (NOW(), %s, %s, %s, %s)",
                      (q[:500], sql, ok, n))
    except Exception:  # noqa: BLE001
        pass


def narrative(metrics):
    """A short daily market narrative; cached once per day in the database."""
    day = datetime.now(IST).strftime("%Y-%m-%d")
    cached = db.get_state(f"narrative_{day}")
    if cached:
        return json.loads(cached)
    text = None
    if enabled():
        try:
            text = _gemini("You are a food-delivery business analyst in India. Write a 3-4 sentence 'market narrative' "
                           "for today's operations dashboard from these figures. Be specific and calm; no hype, no "
                           "bullet points, Indian number formatting with the rupee sign.\n" + json.dumps(metrics, default=str),
                           json_out=False, temperature=0.4)
        except Exception:  # noqa: BLE001
            text = None
    if not text:
        y = metrics.get("yesterday", {})
        text = (f"Yesterday {y.get('orders', 0):,} orders brought in Rs {y.get('revenue', 0):,.0f}, "
                f"{y.get('vs_week', 0):+.0f}% against the 7-day average. {metrics.get('top_state', 'Maharashtra')} led again, "
                f"and {y.get('late_pct', 0):.0f}% of deliveries ran late"
                + (f", with rain in {', '.join(metrics.get('rain_now', [])[:3])}." if metrics.get("rain_now") else "."))
    out = {"date": day, "text": text, "ai": enabled()}
    db.set_state(f"narrative_{day}", json.dumps(out))
    return out
