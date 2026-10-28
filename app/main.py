"""
Food Delivery Live: FastAPI backend for the live website.

  uvicorn app.main:app --host 0.0.0.0 --port 8000

Environment:
  DATABASE_URL     mysql://user:password@host:port/database   (required)
  GEMINI_API_KEY   Google AI Studio key for the "Ask AI" chat  (optional)
  SIMULATE=0       turn the live order simulator off           (optional)

API docs are generated automatically at /docs.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, db, sim

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
STARTED = time.time()

app = FastAPI(title="Food Delivery Live API",
              description="Live orders, analytics and an AI analyst on a MySQL food-delivery database. "
                          "Orders are simulated; the weather is real.",
              version="1.0")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


def _boot():
    try:
        db.setup()
        if os.environ.get("SIMULATE", "1") != "0":
            threading.Thread(target=sim.start, daemon=True, name="simulator").start()
    except Exception as e:  # noqa: BLE001
        db.STATUS.update(state="error", error=str(e)[:400])


@app.on_event("startup")
def startup():
    threading.Thread(target=_boot, daemon=True, name="boot").start()


def _ready():
    if db.STATUS["state"] != "ready":
        raise HTTPException(503, detail={"state": db.STATUS["state"], "detail": db.STATUS["detail"],
                                         "progress": db.STATUS["progress"], "error": db.STATUS["error"]})


_cache = {}


def cached(key, seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _f(v):
    return float(v) if v is not None else 0.0


PLACE = """FROM orders o JOIN restaurants r ON r.restaurant_id = o.restaurant_id
           JOIN zones z ON z.zone_id = r.zone_id JOIN cities ci ON ci.city_id = z.city_id"""


def _filters(state, min_value):
    sql, args = "", []
    if state and state != "All":
        sql += " AND ci.state = %s"
        args.append(state)
    if min_value:
        sql += " AND o.final_amount >= %s"
        args.append(min_value)
    return sql, args


# ---------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    return {"ok": True, "state": db.STATUS["state"], "uptime_s": round(time.time() - STARTED)}


@app.get("/api/status")
def status():
    rain = None
    if db.STATUS["state"] == "ready":
        try:
            rain = json.loads(db.get_state("rain_now", "null") or "null")
        except Exception:  # noqa: BLE001
            rain = None
    return {"setup": db.STATUS, "simulator": sim.STATS, "ai_enabled": ai.enabled(), "rain": rain,
            "server_time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/summary")
def summary(range: str = Query("24h", pattern="^(24h|7d|30d)$"), state: str = "All", min_value: float = 0):
    _ready()

    def build():
        f, a = _filters(state, min_value)
        now = datetime.now(IST).replace(tzinfo=None)
        today = now.date()
        hero = db.one(f"""SELECT COUNT(*) AS orders,
                                 SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END) AS revenue,
                                 AVG(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END) AS aov,
                                 100 * AVG(CASE WHEN o.status = 'DELIVERED' THEN
                                      TIMESTAMPDIFF(SECOND, o.order_time, o.delivered_time) / 60 > o.promised_minutes END) AS late,
                                 SUM(o.status IN ('PLACED','CONFIRMED','PREPARING','PICKED_UP')) AS active,
                                 MAX(o.final_amount) AS biggest
                          {PLACE} WHERE o.order_time >= %s AND o.order_time < %s + INTERVAL 1 DAY {f}""",
                       [today, today] + a)
        busy = db.one("SELECT COUNT(*) AS n FROM delivery_partners WHERE NOT is_available")
        if range == "24h":
            start, fmt, step = now - timedelta(hours=24), "%%Y-%%m-%%d %%H:00", "hour"
        else:
            days = 7 if range == "7d" else 30
            start, fmt, step = datetime(today.year, today.month, today.day) - timedelta(days=days - 1), "%%Y-%%m-%%d", "day"
        series = db.query(f"""SELECT DATE_FORMAT(o.order_time, '{fmt}') AS k, COUNT(*) AS orders,
                                     COALESCE(SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END), 0) AS revenue
                              {PLACE} WHERE o.order_time >= %s {f} GROUP BY k ORDER BY k""", [start] + a)
        by_state = db.query(f"""SELECT ci.state, COUNT(*) AS orders,
                                       COALESCE(SUM(CASE WHEN o.status = 'DELIVERED' THEN o.final_amount END), 0) AS revenue
                                {PLACE} WHERE o.order_time >= %s {f} GROUP BY ci.state ORDER BY revenue DESC""", [start] + a)
        return {"range": range, "step": step, "today": {
                    "orders": int(hero["orders"] or 0), "revenue": round(_f(hero["revenue"])), "aov": round(_f(hero["aov"])),
                    "late_pct": round(_f(hero["late"]), 1), "active": int(hero["active"] or 0),
                    "biggest": round(_f(hero["biggest"])), "riders_busy": int(busy["n"])},
                "series": [{"t": r["k"], "orders": int(r["orders"]), "revenue": round(_f(r["revenue"]))} for r in series],
                "by_state": [{"state": r["state"], "orders": int(r["orders"]), "revenue": round(_f(r["revenue"]))} for r in by_state]}
    return cached(("summary", range, state, min_value), 10, build)


@app.get("/api/live")
def live(state: str = "All", min_value: float = 0, limit: int = Query(30, ge=1, le=100)):
    _ready()

    def build():
        f, a = _filters(state, min_value)
        rows = db.query(f"""SELECT o.order_id, o.order_time, o.status, o.final_amount, o.discount_amount, o.delivered_time,
                                   o.promised_minutes, ci.city_name, ci.state, r.restaurant_name, r.cuisine_type,
                                   z.zone_name, p.payment_method, cp.coupon_code,
                                   (SELECT CONCAT(m.item_name, IF(oi.quantity > 1, CONCAT(' x', oi.quantity), ''))
                                      FROM order_items oi JOIN menu_items m ON m.item_id = oi.item_id
                                     WHERE oi.order_id = o.order_id ORDER BY oi.unit_price * oi.quantity DESC LIMIT 1) AS top_item,
                                   (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id = o.order_id) AS n_items
                            {PLACE} LEFT JOIN payments p ON p.order_id = o.order_id
                            LEFT JOIN coupons cp ON cp.coupon_id = o.coupon_id
                            WHERE o.order_time >= NOW() - INTERVAL 2 DAY {f}
                            ORDER BY o.order_time DESC, o.order_id DESC LIMIT %s""", a + [limit])
        now = datetime.now(IST).replace(tzinfo=None)
        rain = set(sim.STATS.get("rain_now") or [])
        out = []
        for r in rows:
            mins = None
            if r["delivered_time"]:
                mins = round((r["delivered_time"] - r["order_time"]).total_seconds() / 60)
            out.append({"id": r["order_id"], "time": r["order_time"].strftime("%Y-%m-%d %H:%M:%S"),
                        "ago_s": max(0, int((now - r["order_time"]).total_seconds())), "status": r["status"],
                        "amount": round(_f(r["final_amount"])), "discount": round(_f(r["discount_amount"])),
                        "city": r["city_name"], "state": r["state"], "zone": r["zone_name"],
                        "restaurant": r["restaurant_name"], "cuisine": r["cuisine_type"], "item": r["top_item"],
                        "items": int(r["n_items"]), "payment": r["payment_method"], "coupon": r["coupon_code"],
                        "minutes": mins, "late": mins is not None and mins > r["promised_minutes"],
                        "rain": r["city_name"] in rain})
        counts = db.query("SELECT status, COUNT(*) AS n FROM orders WHERE status IN ('PLACED','CONFIRMED','PREPARING','PICKED_UP') "
                          "GROUP BY status")
        return {"orders": out, "in_progress": {r["status"]: int(r["n"]) for r in counts},
                "rain_now": sorted(rain), "rolled_back_today": sim.STATS["rolled_back"],
                "last_rollback": sim.STATS["last_rollback"]}
    return cached(("live", state, min_value, limit), 4, build)


@app.get("/api/states")
def states():
    _ready()
    return cached("states", 3600, lambda: [r["state"] for r in db.query("SELECT DISTINCT state FROM cities ORDER BY state")])


class Ask(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    _ready()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    try:
        return ai.ask(body.question, ip)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Something went wrong: {str(e)[:160]}"}


def _metrics():
    y = db.one(f"""SELECT COUNT(*) AS orders, SUM(CASE WHEN o.status='DELIVERED' THEN o.final_amount END) AS revenue,
                          100*AVG(CASE WHEN o.status='DELIVERED' THEN TIMESTAMPDIFF(SECOND,o.order_time,o.delivered_time)/60 > o.promised_minutes END) AS late
                   FROM orders o WHERE o.order_time >= CURDATE() - INTERVAL 1 DAY AND o.order_time < CURDATE()""")
    wk = db.one("SELECT COUNT(*)/7 AS per_day FROM orders WHERE order_time >= CURDATE() - INTERVAL 8 DAY AND order_time < CURDATE() - INTERVAL 1 DAY")
    top = db.query(f"""SELECT ci.state, COUNT(*) AS n {PLACE} WHERE o.order_time >= CURDATE() - INTERVAL 1 DAY
                       AND o.order_time < CURDATE() GROUP BY ci.state ORDER BY n DESC LIMIT 3""")
    cu = db.query(f"""SELECT r.cuisine_type, COUNT(*) AS n {PLACE} WHERE o.order_time >= CURDATE() - INTERVAL 1 DAY
                      AND o.order_time < CURDATE() GROUP BY r.cuisine_type ORDER BY n DESC LIMIT 3""")
    per_day = _f(wk["per_day"]) or 1
    return {"yesterday": {"orders": int(y["orders"] or 0), "revenue": round(_f(y["revenue"])), "late_pct": round(_f(y["late"]), 1),
                          "vs_week": round(100 * (int(y["orders"] or 0) / per_day - 1), 1)},
            "top_states": [[r["state"], int(r["n"])] for r in top], "top_state": top[0]["state"] if top else None,
            "top_cuisines": [[r["cuisine_type"], int(r["n"])] for r in cu], "rain_now": sim.STATS.get("rain_now") or [],
            "weekday": (datetime.now(IST) - timedelta(days=1)).strftime("%A")}


@app.get("/api/narrative")
def narrative():
    _ready()
    return cached("narrative", 300, lambda: ai.narrative(_metrics()))


@app.get("/api/insights")
def insights():
    _ready()

    def build():
        out = []
        pk = db.one("""SELECT 100*AVG(CASE WHEN HOUR(order_time) IN (13,20,21) THEN TIMESTAMPDIFF(SECOND,order_time,delivered_time)/60 > promised_minutes END) AS peak,
                              100*AVG(CASE WHEN HOUR(order_time) NOT IN (13,20,21) THEN TIMESTAMPDIFF(SECOND,order_time,delivered_time)/60 > promised_minutes END) AS off
                       FROM orders WHERE status='DELIVERED' AND order_time >= NOW() - INTERVAL 30 DAY""")
        out.append({"tag": "Delivery", "title": f"Rush hours run late: {_f(pk['peak']):.0f}% vs {_f(pk['off']):.0f}%",
                    "text": "Orders placed at 1 PM, 8 PM and 9 PM arrive late far more often than at other hours (last 30 days). "
                            "Adding riders at those hours would lift delivery ratings."})
        cod = db.query(f"""SELECT ci.city_name, 100*AVG(p.payment_method='COD') AS cod, COUNT(*) AS n
                           {PLACE} JOIN payments p ON p.order_id = o.order_id
                           WHERE o.order_time >= NOW() - INTERVAL 30 DAY GROUP BY ci.city_name HAVING n >= 40
                           ORDER BY cod DESC LIMIT 1""")
        if cod:
            out.append({"tag": "Payments", "title": f"{cod[0]['city_name']} pays cash most: {_f(cod[0]['cod']):.0f}%",
                        "text": "Cash-on-delivery orders cancel more often. A UPI cashback in smaller cities would cut failed orders."})
        cp = db.query("""SELECT cp.coupon_code, SUM(o.final_amount)/NULLIF(SUM(o.discount_amount),0) AS rpr, COUNT(*) AS n
                         FROM orders o JOIN coupons cp ON cp.coupon_id = o.coupon_id
                         WHERE o.status='DELIVERED' AND o.order_time >= NOW() - INTERVAL 30 DAY
                         GROUP BY cp.coupon_code HAVING n >= 20 ORDER BY rpr""")
        if len(cp) >= 2:
            out.append({"tag": "Coupons", "title": f"{cp[-1]['coupon_code']} returns Rs {_f(cp[-1]['rpr']):.1f} per Rs 1; {cp[0]['coupon_code']} only {_f(cp[0]['rpr']):.1f}",
                        "text": "Revenue earned for every rupee of discount over the last 30 days. Shift budget to the coupons that pay back."})
        rl = db.one("""SELECT AVG(CASE WHEN lb <= 0 THEN delivery_rating END) AS ontime, AVG(CASE WHEN lb > 20 THEN delivery_rating END) AS late20
                       FROM (SELECT rt.delivery_rating, TIMESTAMPDIFF(SECOND,o.order_time,o.delivered_time)/60 - o.promised_minutes AS lb
                             FROM ratings rt JOIN orders o ON o.order_id = rt.order_id WHERE o.order_time >= NOW() - INTERVAL 90 DAY) x""")
        out.append({"tag": "Ratings", "title": f"Late orders drop the rating from {_f(rl['ontime']):.1f}★ to {_f(rl['late20']):.1f}★",
                    "text": "Delivery rating when an order is on time vs more than 20 minutes late (last 90 days)."})
        risk = db.one("SELECT COUNT(*) AS n FROM vw_customer_360 WHERE segment = 'At risk'")
        out.append({"tag": "Customers", "title": f"{int(risk['n']):,} customers are at risk of churning",
                    "text": "They last ordered 31 to 90 days ago. A win-back offer now costs less than winning them back later."})
        if sim.STATS.get("rain_now"):
            rn = sim.STATS["rain_now"]
            out.append({"tag": "Weather", "title": f"Raining now in {len(rn)} {'city' if len(rn) == 1 else 'cities'}",
                        "text": f"{', '.join(rn[:6])}{' and more' if len(rn) > 6 else ''}: riders slow down and cancellations rise. "
                                "Live from Open-Meteo."})
        return out
    return cached("insights", 120, build)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.exception_handler(HTTPException)
def http_err(_req, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
