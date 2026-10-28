"""
Live order simulator.

Every few seconds it:
  * places the orders that "arrive" in this moment through the database's own stored
    procedure sp_place_order (cart check, stock lock, coupon, payment, nearest free rider,
    all in one transaction), so failed orders really roll back;
  * moves active orders along PLACED -> CONFIRMED -> PREPARING -> PICKED_UP -> DELIVERED
    with sp_update_order_status, or cancels some with sp_cancel_order;
  * reads the real rain now in all 49 cities (Open-Meteo) every 30 minutes: rain slows
    riders and causes more cancellations.

If the app was asleep (free hosting sleeps when nobody visits), the missed time is filled
in with finished orders when it wakes, so the history has no gaps.

Order volume, cities, cuisines, opening hours and rush hours follow the same rules as
02_generate_data.py. Orders are simulated; the weather is real.
"""
import bisect
import importlib.util
import json
import math
import os
import random
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pymysql
import requests

from . import db

IST = timezone(timedelta(hours=5, minutes=30))
ACTIVE = ("PLACED", "CONFIRMED", "PREPARING", "PICKED_UP")
MAX_BACKFILL_DAYS = 3

spec = importlib.util.spec_from_file_location("gen", os.path.join(db.ROOT, "02_generate_data.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

STATS = {"placed": 0, "rolled_back": 0, "last_rollback": None, "status_changes": 0, "cancelled": 0,
         "backfilled": 0, "ticks": 0, "last_tick": None, "rain_now": [], "rain_checked": None,
         "mode": None, "errors": 0, "last_error": None}
_lock = threading.Lock()


def now():
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


class World:
    """Reference data and the generator's hidden attributes (quality, popularity, speed ...)."""

    def __init__(self):
        g.random = random.Random(g.SEED)
        phones = set()
        _c, _z, self.czi = g.gen_locations()
        _cu, self.cust_meta = g.gen_customers(self.czi, phones)
        _r, self.rest_meta = g.gen_restaurants(self.czi)
        _m, self.menu_by_rest = g.gen_menu(self.rest_meta)
        _p, self.partners_by_city, self.speed = g.gen_partners(self.czi, phones)
        self.rest_by_id = {r["id"]: r for r in self.rest_meta}
        self.rest_by_city = {}
        for r in self.rest_meta:
            self.rest_by_city.setdefault(r["city"], []).append(r)
        self.menu_cum = {k: list(_cum(p for _i, _pr, p in v)) for k, v in self.menu_by_rest.items()}
        self.zone_city = {zid: cid for cid, zs in self.czi.items() for zid in zs}
        self.city_of_partner = {p: c for c, lst in self.partners_by_city.items() for p, _j in lst}
        self.cust_by_city = {}
        for c in self.cust_meta:
            self.cust_by_city.setdefault(c["city"], []).append(c)
        self.known_customers = {c["id"] for c in self.cust_meta}
        self.refresh_customers()

    def refresh_customers(self):
        """Customers created after the original load (nightly pipeline or live sign-ups)."""
        rows = db.query("SELECT customer_id, zone_id, signup_date FROM customers WHERE customer_id > %s",
                        (max(self.known_customers),))
        for r in rows:
            if r["customer_id"] in self.known_customers:
                continue
            rr = random.Random(f"cust-{r['customer_id']}")
            c = {"id": r["customer_id"], "city": self.zone_city[r["zone_id"]], "zone": r["zone_id"],
                 "signup": r["signup_date"] if isinstance(r["signup_date"], date) else date.fromisoformat(str(r["signup_date"])),
                 "w": min(rr.paretovariate(1.3), 25.0) * (0.05 if rr.random() < 0.30 else 1)}
            self.cust_meta.append(c)
            self.cust_by_city.setdefault(c["city"], []).append(c)
            self.known_customers.add(c["id"])


def _cum(xs):
    s = 0.0
    for x in xs:
        s += x
        yield s


W = None
MODE = {"procs": False, "triggers": False}
RATE = {"daily": 220.0, "computed": None}
COUPONS = {"rows": [], "day": None}


# ---------------------------------------------------------------- weather
def refresh_rain():
    lats = ",".join(str(c[2]) for c in g.CITIES)
    lngs = ",".join(str(c[3]) for c in g.CITIES)
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=precipitation"
           "&timezone=Asia%%2FKolkata" % (lats, lngs))
    try:
        js = requests.get(url, timeout=20).json()
        js = js if isinstance(js, list) else [js]
        rain = [g.CITIES[i][0] for i, loc in enumerate(js) if (loc.get("current", {}).get("precipitation") or 0) >= 0.2]
        STATS["rain_now"] = sorted(rain)
        STATS["rain_checked"] = now().isoformat(sep=" ")
        db.set_state("rain_now", json.dumps({"cities": STATS["rain_now"], "at": STATS["rain_checked"]}))
    except Exception as e:  # noqa: BLE001
        STATS["last_error"] = f"weather: {e}"


def raining(city_idx):
    return g.CITIES[city_idx - 1][0] in STATS["rain_now"]


# ---------------------------------------------------------------- choosing an order
def daily_rate():
    t = now()
    if RATE["computed"] and (t - RATE["computed"]).total_seconds() < 3600:
        return RATE["daily"]
    r = db.one("SELECT COUNT(*) AS n FROM orders WHERE order_time >= %s - INTERVAL 28 DAY AND order_time < %s", (t, t))
    if r and r["n"] > 500:
        RATE["daily"] = r["n"] / 28.0
    RATE["computed"] = t
    return RATE["daily"]


def per_minute(t, region=None, tier=None):
    hw = g.HOUR_WEIGHTS
    dow = g.DOW_WEIGHTS[t.weekday()] / (sum(g.DOW_WEIGHTS.values()) / 7)
    return daily_rate() * hw[t.hour] / sum(hw.values()) * 24 / 1440 * dow * g.SPECIAL_DAYS.get(t.date(), 1.0)


def valid_coupons(d):
    if COUPONS["day"] != d:
        COUPONS["rows"] = db.query("SELECT coupon_id, coupon_code, discount_type, discount_value, max_discount, "
                                   "min_order_value, valid_from, valid_to FROM coupons "
                                   "WHERE is_active AND %s BETWEEN valid_from AND valid_to", (d,))
        COUPONS["day"] = d
    return COUPONS["rows"]


def choose(t, rnd):
    """Pick city, customer, restaurant, basket, coupon and payment for an order at time t."""
    n = len(g.CITIES)
    tt = (t.date() - g.START_DATE).days / (g.END_DATE - g.START_DATE).days
    cw = [g.CITIES[i][4] * g.trend(g.CITIES[i][5], tt) / (1 + 1.6 * tt) * g.festival_boost(g.CITIES[i][1], t.date())
          for i in range(n)]
    city = rnd.choices(range(1, n + 1), weights=cw)[0]
    cname, state, _la, _ln, _s, tier, region, traffic = g.CITIES[city - 1][:8]
    open_r = [r for r in W.rest_by_city[city] if g.is_open(r, t.hour)]
    if not open_r:
        return None
    pool = [c for c in W.cust_by_city.get(city, []) if c["signup"] <= t.date()] or W.cust_by_city.get(city, [])
    cust = rnd.choices(pool, weights=[c["w"] for c in pool])[0]
    aff = g.CUISINE_HOUR.get(g.slot(t.hour), {})
    rest = rnd.choices(open_r, weights=[r["w"] * aff.get(r["cuisine"], 1.0) for r in open_r])[0]
    menu = W.menu_by_rest[rest["id"]]
    k = min(len(menu), rnd.choices([1, 2, 3, 4, 5], weights=[36, 35, 18, 8, 3])[0])
    picked = set()
    while len(picked) < k:
        picked.add(bisect.bisect_left(W.menu_cum[rest["id"]], rnd.random() * W.menu_cum[rest["id"]][-1]))
    items = [(menu[i][0], rnd.choices([1, 2, 3], weights=[80, 17, 3])[0], menu[i][1]) for i in picked]
    item_total = sum(q * p for _i, q, p in items)
    coupon = None
    if rnd.random() < 0.22 + (0.08 if t.weekday() >= 5 else 0):
        ok = [c for c in valid_coupons(t.date()) if float(c["min_order_value"]) <= item_total
              and (c["coupon_code"] != "PUJO30" or state in {"West Bengal", "Tripura", "Assam", "Odisha"})]
        if ok:
            coupon = rnd.choice(ok)
    method = rnd.choices(g.PAYMENT_METHODS, weights=g.PAYMENT_WEIGHTS[tier])[0]
    return dict(city=city, cname=cname, state=state, tier=tier, traffic=traffic, cust=cust, rest=rest,
                items=items, item_total=item_total, coupon=coupon, method=method)


def plan(order_id, partner_id, city, t_order, method, rain):
    """When this order moves to each status (minutes after placing), fixed per order."""
    rnd = random.Random(f"live-{order_id}")
    tier, traffic = g.CITIES[city - 1][5], g.CITIES[city - 1][7]
    spd = W.speed.get(partner_id, 1.0) if partner_id else 1.0
    base = rnd.gauss(24 if tier == 1 else 21, 6)
    total = max(12.0, base * spd * traffic * (1.18 if t_order.hour in (13, 20, 21) else 1.0)
                * (1.06 if t_order.weekday() >= 5 else 1.0) * (1.22 if rain else 1.0) + rnd.gauss(0, 4))
    cancel = rnd.random() < 0.035 + (0.03 if method == "COD" else 0) + (0.02 if rain else 0)
    return {"confirm": 1.0, "prep": rnd.uniform(2, 4), "pick": min(total - 5, rnd.uniform(9, 16)),
            "deliver": total, "cancel": rnd.uniform(2, 6) if cancel else None}


# ---------------------------------------------------------------- write paths
def _log_status(c, order_id, old, new, at):
    if not MODE["triggers"]:
        c.execute("INSERT INTO order_status_log (order_id, old_status, new_status, changed_at) VALUES (%s,%s,%s,%s)",
                  (order_id, old, new, at))


def place_live(o):
    """A real-time order, through the stored procedure when it exists."""
    items_json = json.dumps([{"item_id": i, "qty": q} for i, q, _p in o["items"]])
    code = o["coupon"]["coupon_code"] if o["coupon"] else None
    with db.cursor() as c:
        if MODE["procs"]:
            c.execute("CALL sp_place_order(%s, %s, %s, %s, %s, @oid, @msg)",
                      (o["cust"]["id"], o["rest"]["id"], items_json, code, o["method"]))
            c.execute("SELECT @oid AS oid, @msg AS msg")
            r = c.fetchone()
            oid, msg = r["oid"], r["msg"]
            if oid and not MODE["triggers"]:
                c.execute("SELECT status, order_time FROM orders WHERE order_id = %s", (oid,))
                st = c.fetchone()
                _log_status(c, oid, None, "PLACED", st["order_time"])
                if st["status"] == "CONFIRMED":
                    _log_status(c, oid, "PLACED", "CONFIRMED", st["order_time"])
        else:
            oid, msg = _place_py(c, o, items_json)
    if oid:
        STATS["placed"] += 1
    else:
        STATS["rolled_back"] += 1
        STATS["last_rollback"] = {"at": now().isoformat(sep=" "), "why": (msg or "")[:160]}
        db.set_state("last_rollback", json.dumps(STATS["last_rollback"]))
    return oid


def _place_py(c, o, items_json):
    """Same steps as sp_place_order, as one Python-side transaction (used when the database
    does not allow stored procedures)."""
    conn = c.connection
    conn.begin()
    try:
        ids = [i for i, _q, _p in o["items"]]
        c.execute("SELECT item_id, price, stock_qty FROM menu_items WHERE restaurant_id = %s AND is_available "
                  "AND item_id IN (" + ",".join(["%s"] * len(ids)) + ") FOR UPDATE", [o["rest"]["id"]] + ids)
        got = {r["item_id"]: r for r in c.fetchall()}
        if len(got) != len(ids):
            raise ValueError("An item is not on this menu or unavailable")
        if any(got[i]["stock_qty"] < q for i, q, _p in o["items"]):
            raise ValueError("Not enough stock for one or more items")
        total = round(sum(float(got[i]["price"]) * q for i, q, _p in o["items"]), 2)
        disc, cid = 0.0, None
        cp = o["coupon"]
        if cp:
            cid = cp["coupon_id"]
            v = float(cp["discount_value"])
            disc = min(total * v / 100, float(cp["max_discount"] or total)) if cp["discount_type"] == "PERCENT" else v
            disc = round(min(disc, total), 2)
        fee = 0.0 if total >= 299 else 30.0
        t = now()
        c.execute("INSERT INTO orders (customer_id, restaurant_id, coupon_id, order_time, status, item_total, "
                  "discount_amount, delivery_fee, final_amount, promised_minutes) VALUES (%s,%s,%s,%s,'PLACED',%s,%s,%s,%s,35)",
                  (o["cust"]["id"], o["rest"]["id"], cid, t, total, disc, fee, total - disc + fee))
        oid = c.lastrowid
        _log_status(c, oid, None, "PLACED", t)
        c.executemany("INSERT INTO order_items (order_id, item_id, quantity, unit_price) VALUES (%s,%s,%s,%s)",
                      [(oid, i, q, got[i]["price"]) for i, q, _p in o["items"]])
        for i, q, _p in o["items"]:
            c.execute("UPDATE menu_items SET stock_qty = stock_qty - %s WHERE item_id = %s", (q, i))
        c.execute("INSERT INTO payments (order_id, payment_method, amount, payment_status, paid_at) VALUES (%s,%s,%s,%s,%s)",
                  (oid, o["method"], total - disc + fee, "PENDING" if o["method"] == "COD" else "SUCCESS", t))
        _assign_py(c, oid)
        conn.commit()
        return oid, f"Order {oid} placed"
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return None, f"ROLLED BACK: {e}"


def _assign_py(c, oid):
    c.execute("""SELECT p.partner_id FROM delivery_partners p JOIN zones pz ON pz.zone_id = p.zone_id
                 JOIN orders o ON o.order_id = %s JOIN restaurants r ON r.restaurant_id = o.restaurant_id
                 JOIN zones z ON z.zone_id = r.zone_id
                 WHERE p.is_available AND pz.city_id = z.city_id
                 ORDER BY ST_Distance_Sphere(POINT(p.current_lng, p.current_lat), POINT(z.longitude, z.latitude))
                 LIMIT 1 FOR UPDATE SKIP LOCKED""", (oid,))
    r = c.fetchone()
    if r:
        c.execute("UPDATE delivery_partners SET is_available = FALSE WHERE partner_id = %s", (r["partner_id"],))
        c.execute("UPDATE orders SET partner_id = %s, status = 'CONFIRMED' WHERE order_id = %s", (r["partner_id"], oid))
        _log_status(c, oid, "PLACED", "CONFIRMED", now())
    return r["partner_id"] if r else None


def set_status(c, oid, old, new):
    if MODE["procs"]:
        c.execute("CALL sp_update_order_status(%s, %s, @m)", (oid, new))
    else:
        c.execute("UPDATE orders SET status = %s WHERE order_id = %s AND status = %s", (new, oid, old))
        if new == "DELIVERED":
            c.execute("UPDATE payments SET payment_status = 'SUCCESS', paid_at = %s WHERE order_id = %s "
                      "AND payment_status = 'PENDING'", (now(), oid))
    _log_status(c, oid, old, new, now())
    if not MODE["triggers"] and new == "DELIVERED":
        c.execute("UPDATE delivery_partners p JOIN orders o ON o.partner_id = p.partner_id "
                  "SET p.is_available = TRUE WHERE o.order_id = %s", (oid,))
    STATS["status_changes"] += 1


def cancel(c, oid, old):
    if MODE["procs"]:
        c.execute("CALL sp_cancel_order(%s, @m)", (oid,))
    else:
        conn = c.connection
        conn.begin()
        c.execute("UPDATE orders SET status = 'CANCELLED' WHERE order_id = %s", (oid,))
        c.execute("UPDATE menu_items m JOIN order_items oi ON oi.item_id = m.item_id SET m.stock_qty = m.stock_qty + oi.quantity "
                  "WHERE oi.order_id = %s", (oid,))
        c.execute("UPDATE payments SET payment_status = IF(payment_method = 'COD', 'FAILED', 'REFUNDED') WHERE order_id = %s", (oid,))
        conn.commit()
    _log_status(c, oid, old, "CANCELLED", now())
    if not MODE["triggers"]:
        c.execute("UPDATE delivery_partners p JOIN orders o ON o.partner_id = p.partner_id "
                  "SET p.is_available = TRUE WHERE o.order_id = %s", (oid,))
    STATS["cancelled"] += 1


def rate(c, oid, cust, rest_id, t_order, delivered, rnd):
    late_by = (delivered - t_order).total_seconds() / 60 - 35
    food = round(min(5, max(1, rnd.gauss(W.rest_by_id[rest_id]["q"], 0.8))))
    dv = int(min(5, max(1, 5 - (late_by > 0) - (late_by > 10) - (late_by > 20) + rnd.choice([-1, 0, 0, 0, 1]))))
    try:
        c.execute("INSERT INTO ratings (order_id, customer_id, restaurant_id, food_rating, delivery_rating, review_text, rated_at) "
                  "VALUES (%s,%s,%s,%s,%s,NULL,%s)", (oid, cust, rest_id, food, dv, delivered + timedelta(minutes=rnd.randint(5, 40))))
        if not MODE["triggers"]:
            c.execute("UPDATE restaurants SET total_ratings = total_ratings + 1, avg_rating = ROUND((avg_rating * (total_ratings - 1) + %s) "
                      "/ total_ratings, 1) WHERE restaurant_id = %s", (food, rest_id))
    except pymysql.MySQLError:
        pass


# ---------------------------------------------------------------- moving orders along
def advance(t):
    rows = db.query("""SELECT o.order_id, o.status, o.order_time, o.partner_id, o.customer_id, o.restaurant_id,
                              z.city_id, p.payment_method
                       FROM orders o JOIN restaurants r ON r.restaurant_id = o.restaurant_id
                       JOIN zones z ON z.zone_id = r.zone_id
                       LEFT JOIN payments p ON p.order_id = o.order_id
                       WHERE o.status IN ('PLACED','CONFIRMED','PREPARING','PICKED_UP')""")
    with db.cursor() as c:
        for r in rows:
            oid, st, t0 = r["order_id"], r["status"], r["order_time"]
            pl = plan(oid, r["partner_id"], r["city_id"], t0, r["payment_method"], raining(r["city_id"]))
            el = (t - t0).total_seconds() / 60
            try:
                if pl["cancel"] and el >= pl["cancel"] and st in ("PLACED", "CONFIRMED", "PREPARING"):
                    cancel(c, oid, st)
                    continue
                if st == "PLACED" and el >= pl["confirm"]:
                    if MODE["procs"]:
                        c.execute("CALL sp_assign_partner(%s, @p)", (oid,))
                        c.execute("SELECT @p AS p")
                        if c.fetchone()["p"]:
                            _log_status(c, oid, "PLACED", "CONFIRMED", now())
                            st = "CONFIRMED"
                    elif _assign_py(c, oid):
                        st = "CONFIRMED"
                    if st == "PLACED" and el > 45:          # nobody free for 45 min: give up
                        cancel(c, oid, st)
                        continue
                for nxt, key in (("PREPARING", "prep"), ("PICKED_UP", "pick"), ("DELIVERED", "deliver")):
                    prev = {"PREPARING": "CONFIRMED", "PICKED_UP": "PREPARING", "DELIVERED": "PICKED_UP"}[nxt]
                    if st == prev and el >= pl[key]:
                        if nxt == "DELIVERED":
                            done = t0 + timedelta(minutes=round(pl["deliver"], 1))
                            c.execute("UPDATE orders SET delivered_time = %s WHERE order_id = %s", (done.replace(microsecond=0), oid))
                        set_status(c, oid, st, nxt)
                        st = nxt
                        if nxt == "DELIVERED":
                            rr = random.Random(f"rate-{oid}")
                            if rr.random() < g.RATING_SHARE:
                                rate(c, oid, r["customer_id"], r["restaurant_id"], t0, done, rr)
            except pymysql.MySQLError as e:
                STATS["errors"] += 1
                STATS["last_error"] = f"advance {oid}: {e}"


# ---------------------------------------------------------------- filling gaps while asleep
def backfill(t_from, t_to):
    """Finished orders for a period when the app was not running (e.g. free hosting asleep)."""
    t_from = max(t_from, t_to - timedelta(days=MAX_BACKFILL_DAYS))
    rnd = random.Random(f"backfill-{t_from.isoformat()}")
    rows_o, rows_i, rows_p, rows_r = [], [], [], []
    r = db.one("SELECT MAX(order_id) AS o, (SELECT MAX(order_item_id) FROM order_items) AS i, "
               "(SELECT MAX(payment_id) FROM payments) AS p, (SELECT MAX(rating_id) FROM ratings) AS r FROM orders")
    oid, iid, pid, rid = (r["o"] or 0) + 1, (r["i"] or 0) + 1, (r["p"] or 0) + 1, (r["r"] or 0) + 1
    t = t_from.replace(second=0)
    while t < t_to:
        lam = per_minute(t) * 10
        n = _poisson(lam, rnd)
        for _ in range(n):
            ts = t + timedelta(seconds=rnd.randint(0, 599))
            if ts >= t_to:
                continue
            o = choose(ts, rnd)
            if not o:
                continue
            partner = rnd.choice([p for p, _j in W.partners_by_city[o["city"]]])
            rain = g.is_rainy(o["cname"], o["state"], ts.month) and rnd.random() < 0.45
            pl = plan(oid, partner, o["city"], ts, o["method"], rain)
            total = round(o["item_total"], 2)
            disc = 0.0
            cp = o["coupon"]
            if cp:
                v = float(cp["discount_value"])
                disc = min(total * v / 100, float(cp["max_discount"] or total)) if cp["discount_type"] == "PERCENT" else v
                disc = round(min(disc, total), 2)
            fee = 0.0 if total >= 299 else 30.0
            final = round(total - disc + fee, 2)
            status = "CANCELLED" if pl["cancel"] else "DELIVERED"
            done = None if pl["cancel"] else (ts + timedelta(minutes=round(pl["deliver"], 1))).replace(microsecond=0)
            rows_o.append((oid, o["cust"]["id"], o["rest"]["id"], partner, cp["coupon_id"] if cp else None, ts, status,
                           total, disc, fee, final, 35, done))
            for i, q, p in o["items"]:
                rows_i.append((iid, oid, i, q, p))
                iid += 1
            rows_p.append((pid, oid, o["method"], final,
                           ("FAILED" if o["method"] == "COD" else "REFUNDED") if pl["cancel"] else "SUCCESS", ts))
            pid += 1
            rr = random.Random(f"rate-{oid}")
            if done and rr.random() < g.RATING_SHARE:
                late_by = (done - ts).total_seconds() / 60 - 35
                food = round(min(5, max(1, rr.gauss(o["rest"]["q"], 0.8))))
                dv = int(min(5, max(1, 5 - (late_by > 0) - (late_by > 10) - (late_by > 20) + rr.choice([-1, 0, 0, 0, 1]))))
                rows_r.append((rid, oid, o["cust"]["id"], o["rest"]["id"], food, dv, None, done + timedelta(minutes=rr.randint(10, 600))))
                rid += 1
            oid += 1
        t += timedelta(minutes=10)
    if not rows_o:
        return 0
    with db.cursor() as c:
        conn = c.connection
        conn.begin()
        c.executemany("INSERT INTO orders (order_id, customer_id, restaurant_id, partner_id, coupon_id, order_time, status, "
                      "item_total, discount_amount, delivery_fee, final_amount, promised_minutes, delivered_time) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows_o)
        if not MODE["triggers"]:
            c.executemany("INSERT INTO order_status_log (order_id, old_status, new_status, changed_at) VALUES (%s,NULL,%s,%s)",
                          [(x[0], x[6], x[5]) for x in rows_o])
        c.executemany("INSERT INTO order_items (order_item_id, order_id, item_id, quantity, unit_price) VALUES (%s,%s,%s,%s,%s)", rows_i)
        c.executemany("INSERT INTO payments (payment_id, order_id, payment_method, amount, payment_status, paid_at) "
                      "VALUES (%s,%s,%s,%s,%s,%s)", rows_p)
        if rows_r:
            c.executemany("INSERT INTO ratings (rating_id, order_id, customer_id, restaurant_id, food_rating, delivery_rating, "
                          "review_text, rated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", rows_r)
        conn.commit()
    STATS["backfilled"] += len(rows_o)
    return len(rows_o)


def _poisson(lam, rnd):
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, int(round(rnd.gauss(lam, math.sqrt(lam)))))
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L:
            return k
        k += 1


def signups(t, seconds, rnd):
    lam = 20.0 * seconds / 86400
    for _ in range(_poisson(lam, rnd)):
        n = len(g.CITIES)
        city = rnd.choices(range(1, n + 1), weights=[c[4] * g.trend(c[5], 1.0) for c in g.CITIES])[0]
        g.random = rnd
        name = g.person_name(g.CITIES[city - 1][1], g.CITIES[city - 1][6])
        zone = rnd.choice(W.czi[city])
        for _try in range(5):
            phone = str(rnd.choice([6, 7, 8, 9])) + "".join(rnd.choices("0123456789", k=9))
            try:
                with db.cursor() as c:
                    c.execute("INSERT INTO customers (full_name, email, phone, zone_id, signup_date, is_active) "
                              "VALUES (%s, %s, %s, %s, %s, TRUE)",
                              (name, f"{name.lower().replace(' ', '.')}.{phone[-4:]}{rnd.randint(10, 99)}@gmail.com", phone, zone, t.date()))
                break
            except pymysql.err.IntegrityError:
                continue
    W.refresh_customers()


# ---------------------------------------------------------------- the loop
def tick():
    with _lock:
        t = now()
        last = db.get_state("last_tick")
        if last is None:
            r = db.one("SELECT MAX(order_time) AS t FROM orders")
            last = r["t"] or t
        else:
            last = datetime.fromisoformat(last)
        gap = (t - last).total_seconds()
        if gap > 600:                                   # asleep or restarted: fill the gap
            backfill(last, t - timedelta(minutes=5))
            last = t - timedelta(minutes=5)
            gap = (t - last).total_seconds()
        rnd = random.Random()
        lam = per_minute(t) * gap / 60
        for _ in range(_poisson(lam, rnd)):
            o = choose(t, rnd)
            if o:
                place_live(o)
        advance(t)
        signups(t, gap, rnd)
        if t.minute < 1 and (STATS["ticks"] % 4 == 0):
            with db.cursor() as c:                      # restaurants restock popular dishes
                c.execute("UPDATE menu_items SET stock_qty = stock_qty + 250, is_available = TRUE WHERE stock_qty < 25")
        db.set_state("last_tick", t.isoformat())
        STATS["ticks"] += 1
        STATS["last_tick"] = t.isoformat(sep=" ")


def start(interval=15):
    global W
    W = World()
    MODE["procs"] = db.procedures_available()
    MODE["triggers"] = (db.one("SELECT COUNT(*) AS n FROM information_schema.triggers WHERE trigger_schema = DATABASE()") or {"n": 0})["n"] >= 3
    STATS["mode"] = "stored procedures" if MODE["procs"] else "Python transactions"
    rain_at = 0.0
    while True:
        try:
            if time.time() - rain_at > 1800:
                refresh_rain()
                rain_at = time.time()
            tick()
        except Exception as e:  # noqa: BLE001
            STATS["errors"] += 1
            STATS["last_error"] = f"tick: {e}"
        time.sleep(interval)
