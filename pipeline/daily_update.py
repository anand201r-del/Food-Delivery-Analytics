"""
Daily update: adds every missing day of orders up to yesterday (India time).

What runs each night (GitHub Actions, see .github/workflows/daily-update.yml):
  1. Read the current data from data/*.csv.gz
  2. For each missing day: fetch that day's real rainfall for all 49 cities (Open-Meteo),
     then generate the day's new sign-ups, orders, order items, payments and ratings
     with the same rules as 02_generate_data.py (rush hours, weekends, festivals,
     growth, opening hours, regional food ...). Rain in a city makes its deliveries
     slower, adds a rain fee and causes more cancellations.
  3. Append the rows to data/*.csv.gz and write daily_sql/YYYY-MM-DD.sql so a local
     MySQL copy can catch up (run the files in date order after 04_procedures_triggers.sql).

Orders are simulated; the rainfall is real. Each day uses its own random seed, so a
re-run produces exactly the same rows.

Usage:  python pipeline/daily_update.py [--until YYYY-MM-DD] [--offline]
"""
import argparse
import bisect
import importlib.util
import json
import math
import os
import random
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SQL_DIR = os.path.join(ROOT, "daily_sql")
IST = timezone(timedelta(hours=5, minutes=30))

# reuse the reference data and rules of the main generator
spec = importlib.util.spec_from_file_location("gen", os.path.join(ROOT, "02_generate_data.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

TABLES = ["cities", "zones", "customers", "restaurants", "menu_items", "delivery_partners", "coupons",
          "orders", "order_items", "payments", "ratings"]
APPEND = ["customers", "orders", "order_items", "payments", "ratings"]


def load():
    return {t: pd.read_csv(os.path.join(DATA, f"{t}.csv.gz")) for t in TABLES}


def rebuild_meta():
    """The generator's hidden attributes (restaurant quality and popularity, rider speed,
    customer activity) are not stored in the database. Replaying the first steps of
    02_generate_data.py with the same seed rebuilds them exactly."""
    g.random.seed(g.SEED)
    phones = set()
    _cities, _zones, czi = g.gen_locations()
    _cust, cust_meta = g.gen_customers(czi, phones)
    _rest, rest_meta = g.gen_restaurants(czi)
    _menu, menu_by_rest = g.gen_menu(rest_meta)
    _parts, partners_by_city, speed = g.gen_partners(czi, phones)
    return czi, cust_meta, rest_meta, menu_by_rest, partners_by_city, speed


# ---------------------------------------------------------------- weather
def fetch_rain(days):
    """Real daily rainfall (mm) per city for the given days, from Open-Meteo (free, no key).
    Returns {date: {city_index: mm}}; empty dict if the service can't be reached."""
    lats = ",".join(str(c[2]) for c in g.CITIES)
    lngs = ",".join(str(c[3]) for c in g.CITIES)
    start, end = min(days), max(days)
    out = {}
    urls = [
        "https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}&daily=precipitation_sum"
        "&timezone=Asia%2FKolkata&start_date={}&end_date={}".format(lats, lngs, start, end),
        "https://archive-api.open-meteo.com/v1/archive?latitude={}&longitude={}&daily=precipitation_sum"
        "&timezone=Asia%2FKolkata&start_date={}&end_date={}".format(lats, lngs, start, end),
    ]
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                js = json.loads(r.read().decode())
            js = js if isinstance(js, list) else [js]
            for ci, loc in enumerate(js):
                for ds, mm in zip(loc["daily"]["time"], loc["daily"]["precipitation_sum"]):
                    out.setdefault(date.fromisoformat(ds), {})[ci] = mm
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            print(f"  weather source failed ({url.split('/')[2]}): {e}")
    return out


# ---------------------------------------------------------------- one day
def sql_rows(table, rows):
    head = g.INSERTS[table].split(" VALUES")[0]
    vals = ",\n".join("(" + ",".join(g.sql_literal(v) for v in r) + ")" for r in rows)
    return f"{head} VALUES\n{vals};\n" if rows else ""


def gen_day(d, D, meta, rain_mm, base_daily):
    czi, cust_meta, rest_meta, menu_by_rest, partners_by_city, speed = meta
    rnd = random.Random(f"{g.SEED}-{d.isoformat()}")
    g.random = rnd                      # the generator helpers draw from this day's stream
    ncity = len(g.CITIES)

    # ---- new sign-ups (about 20 a day, more in growing cities)
    cust = D["customers"]
    next_cid = int(cust.customer_id.max()) + 1
    used_phone = set(cust.phone.astype(str)) | set(D["delivery_partners"].phone.astype(str))
    shares = [c[4] for c in g.CITIES]
    new_c = []
    for _ in range(max(0, int(rnd.gauss(20, 4)))):
        city = rnd.choices(range(1, ncity + 1), weights=[s * g.trend(c[5], 1.0) for s, c in zip(shares, g.CITIES)])[0]
        name_, state, *_r = g.CITIES[city - 1]
        zone = rnd.choice(czi[city])
        nm = g.person_name(state, g.CITIES[city - 1][6])
        while True:
            ph = str(rnd.choice([6, 7, 8, 9])) + "".join(rnd.choices("0123456789", k=9))
            if ph not in used_phone:
                used_phone.add(ph)
                break
        email = f"{nm.lower().replace(' ', '.')}{next_cid}@{rnd.choice(['gmail.com', 'gmail.com', 'gmail.com', 'yahoo.in', 'outlook.com', 'rediffmail.com'])}"
        act = min(rnd.paretovariate(1.3), 25.0) * (0.05 if rnd.random() < 0.30 else 1)
        new_c.append((next_cid, nm, email, ph, zone, d, True))
        cust_meta.append({"id": next_cid, "city": city, "zone": zone, "signup": d, "w": act})
        next_cid += 1

    # ---- how many orders today
    t = (d - g.START_DATE).days / (g.END_DATE - g.START_DATE).days
    seasonal = 1.0 + 0.08 * math.sin(2 * math.pi * (d.timetuple().tm_yday / 365.0) + 1.2)
    months_on = (d - g.END_DATE).days / 30.4
    dow = g.DOW_WEIGHTS[d.weekday()] / (sum(g.DOW_WEIGHTS.values()) / 7)
    expected = base_daily * (1.015 ** months_on) * seasonal * dow * g.SPECIAL_DAYS.get(d, 1.0)
    n_orders = max(1, int(round(rnd.gauss(expected, math.sqrt(expected)))))

    city_w = [shares[i] * g.trend(g.CITIES[i][5], t) / (1.0 + 1.6 * t) * g.festival_boost(g.CITIES[i][1], d)
              for i in range(ncity)]
    rest_by_city = {}
    for r in rest_meta:
        rest_by_city.setdefault(r["city"], []).append(r)
    by_city = {}
    for c in cust_meta:
        if c["signup"] <= d:
            by_city.setdefault(c["city"], []).append(c)
    hours = list(range(24))
    coupons = [tuple(x) for x in D["coupons"].itertuples(index=False)]
    coupons = [(c[0], c[1], c[2], c[3], None if pd.isna(c[4]) else c[4], c[5],
                date.fromisoformat(str(c[6])), date.fromisoformat(str(c[7])), c[8]) for c in coupons]
    cweights = [c[7] for c in g.COUPONS]
    ipl = any(a <= d <= b for a, b in g.IPL_SEASONS)

    oid = int(D["orders"].order_id.max()) + 1
    oiid = int(D["order_items"].order_item_id.max()) + 1
    rid_ = int(D["ratings"].rating_id.max()) + 1
    orders, items, pays, rates = [], [], [], []
    menu_cum = {k: [sum(p for _i, _pr, p in v[:j + 1]) for j in range(len(v))] for k, v in menu_by_rest.items()}
    for _ in range(n_orders):
        city = rnd.choices(range(1, ncity + 1), weights=city_w)[0]
        cname, state, _la, _ln, _s, tier, region, traffic = g.CITIES[city - 1][:8]
        hw = g.hour_weights_for(region, tier)
        hw = [hw[h] * (1.18 if ipl and 19 <= h <= 23 else 1.0) for h in hours]
        hour = rnd.choices(hours, weights=hw)[0]
        open_r = [r for r in rest_by_city[city] if g.is_open(r, hour)]
        if not open_r:
            hour = rnd.choice([19, 20, 21])
            open_r = [r for r in rest_by_city[city] if g.is_open(r, hour)] or rest_by_city[city]
        ts = datetime(d.year, d.month, d.day, hour, rnd.randint(0, 59), rnd.randint(0, 59))
        pool = by_city.get(city) or [c for c in cust_meta if c["city"] == city]
        cust_ = rnd.choices(pool, weights=[c["w"] for c in pool])[0]
        aff = g.CUISINE_HOUR.get(g.slot(hour), {})
        rest = rnd.choices(open_r, weights=[r["w"] * aff.get(r["cuisine"], 1.0) for r in open_r])[0]

        menu = menu_by_rest[rest["id"]]
        k = min(len(menu), rnd.choices([1, 2, 3, 4, 5], weights=[36, 35, 18, 8, 3])[0])
        picked = set()
        while len(picked) < k:
            picked.add(bisect.bisect_left(menu_cum[rest["id"]], rnd.random() * menu_cum[rest["id"]][-1]))
        item_total, basket = 0.0, []
        for idx in picked:
            iid, price, _p = menu[idx]
            q = rnd.choices([1, 2, 3], weights=[80, 17, 3])[0]
            item_total += price * q
            basket.append((oiid, oid, iid, q, price))
            oiid += 1
        item_total = round(item_total, 2)

        coupon_id, discount = None, 0.0
        if rnd.random() < 0.22 + (0.08 if d.weekday() >= 5 else 0) + (0.05 if d in g.SPECIAL_DAYS else 0):
            c = rnd.choices(coupons, weights=cweights)[0]
            if c[1] == "PUJO30" and state not in {"West Bengal", "Tripura", "Assam", "Odisha"}:
                c = None
            if c and c[6] <= d <= c[7] and item_total >= c[5]:
                coupon_id, discount = int(c[0]), g.compute_discount(c, item_total)

        mm = rain_mm.get(city - 1)
        rain = (mm is not None and mm >= 2.5) if rain_mm else (g.is_rainy(cname, state, d.month) and rnd.random() < 0.45)
        if rnd.random() < 0.35 and item_total >= 199:
            fee = 0.0
        else:
            fee = float(rnd.choice([19, 25, 29, 35, 39]) if tier == 1 else rnd.choice([15, 19, 25, 29]))
            fee += 20 if rain else 0
            fee += 15 if (hour >= 23 or hour <= 4) else 0
        final = round(max(item_total - discount, 0) + fee, 2)
        plist = [p for p, j in partners_by_city[city] if j <= d] or [p for p, _j in partners_by_city[city]]
        partner = rnd.choice(plist)
        method = rnd.choices(g.PAYMENT_METHODS, weights=g.PAYMENT_WEIGHTS[tier])[0]
        cancelled = rnd.random() < 0.035 + (0.03 if method == "COD" else 0) + (0.02 if rain else 0)
        promised = rnd.choice([30, 35, 35, 40, 45] if tier == 1 else [25, 30, 30, 35, 40])
        dt = None
        if not cancelled:
            base = rnd.gauss(24 if tier == 1 else 21, 6)
            mins = max(10, base * speed[partner] * traffic * (1.18 if hour in (13, 20, 21) else 1.0)
                       * (1.06 if d.weekday() >= 5 else 1.0) * (1.22 if rain else 1.0) + rnd.gauss(0, 4))
            dt = (ts + timedelta(minutes=round(mins, 1))).replace(microsecond=0)
        orders.append((oid, cust_["id"], rest["id"], partner, coupon_id, ts, "CANCELLED" if cancelled else "DELIVERED",
                       item_total, discount, fee, final, promised, dt))
        items.extend(basket)
        pays.append((oid, oid, method, final, ("FAILED" if method == "COD" else "REFUNDED") if cancelled else "SUCCESS", ts))
        if not cancelled and rnd.random() < g.RATING_SHARE:
            late_by = (dt - ts).total_seconds() / 60 - promised
            food = round(min(5, max(1, rnd.gauss(rest["q"], 0.8))))
            dv = 5 - (late_by > 0) - (late_by > 10) - (late_by > 20)
            dv = int(min(5, max(1, dv + rnd.choice([-1, 0, 0, 0, 1]))))
            review = rnd.choice({5: ["Amazing food!", "Loved it, will order again", "Best in town", "Super tasty and hot"],
                                 4: ["Good food", "Tasty, quantity could be more", "Nice packaging", "Worth the price"],
                                 3: ["Average", "Okay okay", "Food was cold", "Too oily"],
                                 2: ["Not good", "Delivery was late", "Too salty", "Missing items"],
                                 1: ["Terrible experience", "Wrong order delivered", "Very late and cold food"]}[food]) \
                if rnd.random() < 0.25 else None
            rates.append((rid_, oid, cust_["id"], rest["id"], food, dv, review, dt + timedelta(minutes=rnd.randint(10, 600))))
            rid_ += 1
        oid += 1
    rainy = sorted(g.CITIES[ci][0] for ci, mm in rain_mm.items() if mm is not None and mm >= 2.5)
    return {"customers": new_c, "orders": orders, "order_items": items, "payments": pays, "ratings": rates}, rainy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", help="last day to add (default: yesterday, India time)")
    ap.add_argument("--offline", action="store_true", help="skip the weather download (use seasonal rain rules)")
    a = ap.parse_args()
    until = date.fromisoformat(a.until) if a.until else (datetime.now(IST).date() - timedelta(days=1))
    D = load()
    last = pd.to_datetime(D["orders"].order_time).max().date()
    days = [last + timedelta(days=i) for i in range(1, (until - last).days + 1)]
    if not days:
        print(f"Up to date (last order day {last}).")
        return
    print(f"Adding {len(days)} day(s): {days[0]} .. {days[-1]}")
    meta = rebuild_meta()
    # customers who signed up through the daily job get their activity weight back (seeded per id)
    known = {c["id"] for c in meta[1]}
    for row in D["customers"].itertuples(index=False):
        if row.customer_id not in known:
            rr = random.Random(f"cust-{row.customer_id}")
            zone_city = dict(zip(D["zones"].zone_id, D["zones"].city_id))
            meta[1].append({"id": int(row.customer_id), "city": int(zone_city[row.zone_id]), "zone": int(row.zone_id),
                            "signup": date.fromisoformat(str(row.signup_date)),
                            "w": min(rr.paretovariate(1.3), 25.0) * (0.05 if rr.random() < 0.30 else 1)})
    weather = {} if a.offline else fetch_rain(days)
    # typical daily volume = average of the last 28 days in the data
    ot = pd.to_datetime(D["orders"].order_time)
    recent = ot[ot > pd.Timestamp(last) - pd.Timedelta(days=28)]
    base_daily = len(recent) / 28 / (1.015 ** (((last - g.END_DATE).days) / 30.4))
    log = []
    os.makedirs(SQL_DIR, exist_ok=True)
    for d in days:
        new, rainy = gen_day(d, D, meta, weather.get(d, {}), base_daily)
        cols = {t: list(D[t].columns) for t in APPEND}
        for t in APPEND:
            if new[t]:
                D[t] = pd.concat([D[t], pd.DataFrame(new[t], columns=cols[t])], ignore_index=True)
        with open(os.path.join(SQL_DIR, f"{d.isoformat()}.sql"), "w", encoding="utf-8") as fh:
            fh.write(f"-- Daily update for {d.isoformat()} (generated by pipeline/daily_update.py)\n"
                     f"-- Real rain (2.5 mm or more) in: {', '.join(rainy) if rainy else 'no city' if weather else 'weather unavailable'}\n"
                     "USE food_delivery;\nSTART TRANSACTION;\n")
            for t in APPEND:
                fh.write(sql_rows(t, new[t]))
            fh.write("COMMIT;\n")
        rev = sum(o[10] for o in new["orders"] if o[6] == "DELIVERED")
        log.append({"date": d.isoformat(), "orders": len(new["orders"]), "revenue": round(rev),
                    "new_customers": len(new["customers"]), "rain_cities": rainy, "weather": bool(weather.get(d))})
        print(f"  {d}: {len(new['orders'])} orders, Rs {rev:,.0f}, {len(new['customers'])} sign-ups, "
              f"rain in {len(rainy)} cities{'' if weather.get(d) else ' (no weather data)'}")
    for t in APPEND:
        D[t].to_csv(os.path.join(DATA, f"{t}.csv.gz"), index=False, compression="gzip")
    lp = os.path.join(DATA, "daily_log.json")
    old = json.load(open(lp)) if os.path.exists(lp) else []
    json.dump((old + log)[-400:], open(lp, "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())
