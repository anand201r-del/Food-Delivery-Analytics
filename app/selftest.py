"""End-to-end test against a real MySQL (run by .github/workflows/live-app-test.yml)."""
import json
import os
import random
import sys
import time
from datetime import timedelta

os.environ.setdefault("SIMULATE", "0")
problems, notes = [], []
try:
    from fastapi.testclient import TestClient  # noqa: E402

    from app import ai, db, main, sim  # noqa: E402
except Exception:  # noqa: BLE001
    import traceback
    print("::error::import failed: " + traceback.format_exc()[-1500:].replace("%", "%25").replace("\n", "%0A"))
    raise


def check(cond, msg):
    (notes if cond else problems).append(("OK " if cond else "FAIL ") + msg)


def run():
    t0 = time.time()
    db.setup()
    check(db.STATUS["state"] == "ready", f"setup finished in {round(time.time() - t0)} s")
    n = db.one("SELECT (SELECT COUNT(*) FROM orders) o, (SELECT COUNT(*) FROM order_items) i, (SELECT COUNT(*) FROM customers) c, "
               "(SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema = DATABASE()) r, "
               "(SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema = DATABASE()) t, "
               "(SELECT COUNT(*) FROM information_schema.views WHERE table_schema = DATABASE()) v")
    check(n["o"] >= 100000, f"loaded {n['o']:,} orders, {n['i']:,} items, {n['c']:,} customers; routines {n['r']}, triggers {n['t']}, views {n['v']}")
    notes.append("setup notes: " + str(db.get_state("setup_notes")))

    sim.W = sim.World()
    sim.MODE["procs"] = db.procedures_available()
    sim.MODE["triggers"] = n["t"] >= 3
    notes.append(f"mode: procedures={sim.MODE['procs']} triggers={sim.MODE['triggers']} reader_user={bool(db.get_state('reader_pw'))}")

    # first tick: fills the gap since the last order in the data, then places live orders
    before = n["o"]
    sim.tick()
    after = db.one("SELECT COUNT(*) AS n FROM orders")["n"]
    check(after > before, f"first tick backfilled/placed {after - before} orders (backfilled {sim.STATS['backfilled']})")

    # place 40 live orders now
    rnd = random.Random(7)
    placed = []
    t = sim.now()
    for _ in range(80):
        o = sim.choose(t.replace(hour=20), rnd)
        if o:
            oid = sim.place_live(o)
            if oid:
                placed.append(oid)
        if len(placed) >= 40:
            break
    check(len(placed) >= 30, f"placed {len(placed)} live orders through {'sp_place_order' if sim.MODE['procs'] else 'Python transactions'}")

    # a cart that must roll back (too much stock)
    o = sim.choose(t.replace(hour=20), rnd)
    o["items"] = [(o["items"][0][0], 100000, o["items"][0][2])]
    cnt = db.one("SELECT COUNT(*) AS n FROM orders")["n"]
    bad = sim.place_live(o)
    check(bad is None and db.one("SELECT COUNT(*) AS n FROM orders")["n"] == cnt,
          f"impossible order rolled back cleanly: {sim.STATS['last_rollback']}")

    # move time forward: everything should finish
    sim.advance(sim.now() + timedelta(minutes=8))
    mid = db.query("SELECT status, COUNT(*) AS n FROM orders WHERE order_id IN (%s) GROUP BY status" % ",".join(map(str, placed)))
    sim.advance(sim.now() + timedelta(minutes=200))
    end = db.query("SELECT status, COUNT(*) AS n FROM orders WHERE order_id IN (%s) GROUP BY status" % ",".join(map(str, placed)))
    check(all(r["status"] in ("DELIVERED", "CANCELLED") for r in end), f"statuses after 8 min {mid}; after 200 min {end}")
    logs = db.one("SELECT COUNT(*) AS n FROM order_status_log WHERE order_id IN (%s)" % ",".join(map(str, placed)))["n"]
    check(logs >= 3 * len(placed), f"status log rows for live orders: {logs}")
    busy = db.one("SELECT COUNT(*) AS n FROM delivery_partners WHERE NOT is_available")["n"]
    check(busy <= 5, f"riders still marked busy after all deliveries: {busy}")

    # API
    with TestClient(main.app) as cl:
        for _ in range(60):
            if cl.get("/api/health").json()["state"] == "ready":
                break
            time.sleep(0.5)
        for path in ["/api/health", "/api/status", "/api/states", "/api/summary?range=24h", "/api/summary?range=7d&state=Kerala",
                     "/api/summary?range=30d&min_value=1000", "/api/live", "/api/live?state=Maharashtra&min_value=500",
                     "/api/narrative", "/api/insights", "/", "/docs"]:
            r = cl.get(path)
            check(r.status_code == 200, f"GET {path} -> {r.status_code}" + ("" if r.status_code == 200 else " " + r.text[:200]))
        s = cl.get("/api/summary?range=24h").json()
        notes.append("today: " + json.dumps(s["today"]) + f"; series points {len(s['series'])}; states {len(s['by_state'])}")
        lv = cl.get("/api/live").json()
        notes.append(f"live feed rows {len(lv['orders'])}, in progress {lv['in_progress']}, first: " + json.dumps(lv["orders"][0])[:300])
        a = cl.post("/api/ask", json={"question": "Which state has the most orders today?"}).json()
        check("rows" in a and a["rows"], f"ask (saved example, no AI key): {str(a)[:200]}")
        a = cl.post("/api/ask", json={"question": "anything else"}).json()
        check("error" in a, f"ask without key gives a friendly message: {a.get('error')}")
        notes.append("narrative: " + cl.get("/api/narrative").json()["text"][:200])
        notes.append("insights: " + " | ".join(x["title"] for x in cl.get("/api/insights").json()))

    for bad_sql in ["DELETE FROM orders", "SELECT 1; DROP TABLE orders", "SELECT SLEEP(10)", "UPDATE orders SET status='X'"]:
        try:
            ai.safe_sql(bad_sql)
            problems.append("FAIL safe_sql accepted: " + bad_sql)
        except ValueError:
            pass
    check(ai.safe_sql("WITH x AS (SELECT 1 AS a) SELECT * FROM x").endswith("LIMIT 50"), "safe_sql accepts WITH and adds LIMIT")
    cols, rows, ms = ai.run_readonly("SELECT COUNT(*) AS n FROM orders")
    check(rows[0]["n"] > 0, f"read-only connection works ({ms} ms)")
    try:
        conn, is_reader = db.reader_connection()
        with conn.cursor() as c:
            c.execute("DELETE FROM coupons WHERE coupon_id = -1")
        problems.append("FAIL read-only connection allowed a DELETE")
    except Exception as e:  # noqa: BLE001
        notes.append(f"read-only connection blocked a write: {str(e)[:90]}")



try:
    run()
except Exception:  # noqa: BLE001
    import traceback
    tb = traceback.format_exc()
    problems.append('FAIL crashed: ' + tb[-1500:])

out = notes + problems
for i in range(0, len(out), 6):
    chunk = " || ".join(out[i:i + 6])
    print(("::error::" if any(x.startswith("FAIL") for x in out[i:i + 6]) else "::notice::") + chunk.replace("%", "%25").replace("\r", "").replace("\n", "%0A"))
print("\n".join(out))
sys.exit(1 if problems else 0)
