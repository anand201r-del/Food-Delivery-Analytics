"""MySQL connection, one-time setup (schema, data, logic, views, indexes) and small helpers."""
import csv
import gzip
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, unquote, urlparse

import pymysql
import pymysql.cursors

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = "+05:30"                      # all timestamps in the data are India time

LOAD_ORDER = ["cities", "zones", "customers", "restaurants", "menu_items", "delivery_partners",
              "coupons", "orders", "order_items", "payments", "ratings"]
BOOL_COLS = {"is_active", "is_veg_only", "is_veg", "is_available"}

STATUS = {"state": "starting", "detail": "", "progress": 0.0, "procedures": None, "error": None}


def _params():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    u = urlparse(url)
    q = parse_qs(u.query)
    db = (u.path or "/defaultdb").lstrip("/") or "defaultdb"
    p = dict(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
             password=unquote(u.password or ""), database=db, charset="utf8mb4",
             autocommit=True, connect_timeout=20, read_timeout=120, write_timeout=120,
             init_command=f"SET time_zone = '{TZ}'")
    ssl_mode = (q.get("ssl-mode") or q.get("ssl_mode") or [""])[0].upper()
    if os.environ.get("DB_SSL", "1") != "0" and ssl_mode != "DISABLED":
        # encrypted connection; the managed database's certificate is not pinned here
        p["ssl"] = {"fake_flag_to_enable_tls": True}
    return p


def connect(**over):
    p = _params()
    p.update(over)
    return pymysql.connect(**p)


_local = threading.local()


@contextmanager
def cursor(dict_rows=True):
    """A cursor on a per-thread connection that reconnects if the database went away."""
    conn = getattr(_local, "conn", None)
    if conn is None or not conn.open:
        conn = _local.conn = connect()
    else:
        try:
            conn.ping(reconnect=True)
        except Exception:  # noqa: BLE001
            conn = _local.conn = connect()
    cur = conn.cursor(pymysql.cursors.DictCursor if dict_rows else pymysql.cursors.Cursor)
    try:
        yield cur
    finally:
        cur.close()


def query(sql, args=None):
    with cursor() as c:
        c.execute(sql, args)
        return c.fetchall()


def one(sql, args=None):
    rows = query(sql, args)
    return rows[0] if rows else None


# ---------------------------------------------------------------- SQL file runner
def split_sql(text):
    """Split a .sql file into statements, honouring DELIMITER blocks (for triggers and procedures)."""
    out, buf, delim = [], [], ";"
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"(?i)^DELIMITER\s+(\S+)", s)
        if m:
            delim = m.group(1)
            continue
        if not buf and (not s or s.startswith("--")):
            continue
        buf.append(line)
        if s.endswith(delim):
            stmt = "\n".join(buf).rstrip()
            stmt = stmt[: len(stmt) - len(delim)].strip()
            if stmt:
                out.append(stmt)
            buf = []
    if buf and "\n".join(buf).strip():
        out.append("\n".join(buf).strip())
    return out


def run_file(name, cut_at=None, skip=()):
    text = open(os.path.join(ROOT, name), encoding="utf-8").read()
    if cut_at and cut_at in text:
        text = text[: text.index(cut_at)]
    done, failed = 0, []
    with cursor() as c:
        for st in split_sql(text):
            head = st.lstrip().upper()
            if head.startswith(("USE ", "DROP DATABASE", "CREATE DATABASE")) or any(head.startswith(k) for k in skip):
                continue
            try:
                c.execute(st)
                done += 1
            except pymysql.MySQLError as e:
                failed.append((st.split("\n")[0][:80], str(e)))
    return done, failed


# ---------------------------------------------------------------- setup
def tables_ready():
    try:
        r = one("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() "
                "AND table_name = 'sim_state'")
        return bool(r and r["n"]) and one("SELECT v FROM sim_state WHERE k = 'setup_done'") is not None
    except pymysql.MySQLError:
        return False


def _rows(table):
    path = os.path.join(ROOT, "data", f"{table}.csv.gz")
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        r = csv.reader(fh)
        cols = next(r)
        bools = [i for i, c in enumerate(cols) if c in BOOL_COLS]
        for row in r:
            vals = [None if v == "" else v for v in row]
            for i in bools:
                if vals[i] is not None:
                    vals[i] = 1 if vals[i] in ("True", "1", "true") else 0
            yield cols, vals


def load_data():
    total = {"orders": 0}
    sizes = {}
    for t in LOAD_ORDER:
        with gzip.open(os.path.join(ROOT, "data", f"{t}.csv.gz"), "rt", encoding="utf-8") as fh:
            sizes[t] = sum(1 for _ in fh) - 1
    all_rows = sum(sizes.values())
    loaded = 0
    with cursor() as c:
        c.execute("SET FOREIGN_KEY_CHECKS = 0")
        c.execute("SET UNIQUE_CHECKS = 0")
        for t in LOAD_ORDER:
            c.execute(f"DELETE FROM {t}")
            batch, cols = [], None
            for cols, vals in _rows(t):
                batch.append(vals)
                if len(batch) >= 2000:
                    _insert(c, t, cols, batch)
                    loaded += len(batch)
                    batch = []
                    STATUS["progress"] = round(loaded / all_rows, 3)
                    STATUS["detail"] = f"loading {t} ({loaded:,} of {all_rows:,} rows)"
            if batch:
                _insert(c, t, cols, batch)
                loaded += len(batch)
            total[t] = sizes[t]
        c.execute("SET FOREIGN_KEY_CHECKS = 1")
        c.execute("SET UNIQUE_CHECKS = 1")
        c.execute("""UPDATE restaurants r
                     JOIN (SELECT restaurant_id, ROUND(AVG(food_rating), 1) AS a, COUNT(*) AS n
                           FROM ratings GROUP BY restaurant_id) x ON x.restaurant_id = r.restaurant_id
                     SET r.avg_rating = x.a, r.total_ratings = x.n""")
    return total


def _insert(c, table, cols, rows):
    ph = ",".join(["%s"] * len(cols))
    c.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", rows)


INDEXES = [
    ("orders", "idx_orders_status_time", "status, order_time, final_amount"),
    ("orders", "idx_orders_time", "order_time"),
    ("orders", "idx_orders_customer_time", "customer_id, order_time"),
    ("orders", "idx_orders_status_restaurant", "status, restaurant_id, final_amount"),
    ("menu_items", "idx_menu_restaurant_available", "restaurant_id, is_available"),
    ("delivery_partners", "idx_partners_available_zone", "is_available, zone_id"),
    ("customers", "idx_customers_signup", "signup_date"),
    ("ratings", "idx_ratings_restaurant_food", "restaurant_id, food_rating"),
]


def setup(force=False):
    """Create everything once. Safe to call on every start: it returns quickly when done."""
    if not force and tables_ready():
        STATUS["procedures"] = procedures_available()
        STATUS.update(state="ready", detail="", progress=1.0)
        return
    STATUS.update(state="setting up", detail="checking database", progress=0.0)
    t0 = time.time()
    STATUS["detail"] = "creating tables (01_schema.sql)"
    with cursor() as c:
        c.execute("SET FOREIGN_KEY_CHECKS = 0")
        for t in ["order_status_log"] + LOAD_ORDER[::-1] + ["sim_state", "ai_log"]:
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.execute("SET FOREIGN_KEY_CHECKS = 1")
    _, failed = run_file("01_schema.sql")
    if failed:
        raise RuntimeError(f"schema failed: {failed[:3]}")
    with cursor() as c:
        c.execute("CREATE TABLE sim_state (k VARCHAR(40) PRIMARY KEY, v TEXT, updated_at TIMESTAMP "
                  "DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE ai_log (id BIGINT AUTO_INCREMENT PRIMARY KEY, asked_at DATETIME NOT NULL, "
                  "question VARCHAR(500) NOT NULL, sql_text TEXT, ok BOOLEAN, rows_returned INT)")
    load_data()
    STATUS["detail"] = "creating indexes"
    with cursor() as c:
        for table, name, cols in INDEXES:
            try:
                c.execute(f"CREATE INDEX {name} ON {table} ({cols})")
            except pymysql.MySQLError:
                pass
    STATUS["detail"] = "creating triggers and stored procedures (04_procedures_triggers.sql)"
    _, failed = run_file("04_procedures_triggers.sql", cut_at="-- DEMO")
    proc_errors = [f for f in failed if "DROP" not in f[0].upper()]
    STATUS["detail"] = "creating reporting views (06_views.sql)"
    run_file("06_views.sql", cut_at="-- Try them", skip=("SELECT",))
    reader_pw = secrets.token_urlsafe(18)
    with cursor() as c:
        try:
            c.execute("DROP USER IF EXISTS 'fd_reader'@'%'")
            c.execute("CREATE USER 'fd_reader'@'%%' IDENTIFIED BY %s", (reader_pw,))
            c.execute("GRANT SELECT ON `%s`.* TO 'fd_reader'@'%%'" % _params()["database"])
            c.execute("INSERT INTO sim_state (k, v) VALUES ('reader_pw', %s) ON DUPLICATE KEY UPDATE v = VALUES(v)", (reader_pw,))
        except pymysql.MySQLError:
            pass                    # the AI chat then falls back to read-only transactions
        c.execute("REPLACE INTO sim_state (k, v) VALUES ('setup_done', %s)", (time.strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("REPLACE INTO sim_state (k, v) VALUES ('setup_notes', %s)",
                  (f"{round(time.time() - t0)} s; procedure errors: {len(proc_errors)}"
                   + (f" ({proc_errors[0][1][:200]})" if proc_errors else ""),))
    STATUS["procedures"] = procedures_available()
    STATUS.update(state="ready", detail="", progress=1.0)


def procedures_available():
    r = one("SELECT COUNT(*) AS n FROM information_schema.routines WHERE routine_schema = DATABASE() "
            "AND routine_name IN ('sp_place_order','sp_update_order_status','sp_cancel_order','sp_assign_partner')")
    return bool(r and r["n"] == 4)


def get_state(k, default=None):
    r = one("SELECT v FROM sim_state WHERE k = %s", (k,))
    return r["v"] if r else default


def set_state(k, v):
    with cursor() as c:
        c.execute("INSERT INTO sim_state (k, v) VALUES (%s, %s) ON DUPLICATE KEY UPDATE v = VALUES(v)", (k, str(v)))


def reader_connection():
    """Connection for AI-written SQL: SELECT-only user when available, else a read-only session."""
    pw = get_state("reader_pw")
    if pw:
        try:
            return connect(user="fd_reader", password=pw, read_timeout=15), True
        except pymysql.MySQLError:
            pass
    conn = connect(read_timeout=15)
    with conn.cursor() as c:
        c.execute("SET SESSION TRANSACTION READ ONLY")
    return conn, False
