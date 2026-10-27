#!/usr/bin/env python3
"""
Atikur SMM Panel — panel.atikur.cyou (v1)

Customer: signup/login, add balance via bKash (manual TrxID), place orders, track status.
Admin: approve/reject bKash payments, view all orders, sync status from provider API.

CONFIG — edit these (or set env vars) before going live:
"""
import os
import sqlite3
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------- CONFIG ----------------
BKASH_NUMBER   = os.environ.get("BKASH_NUMBER", "01723502619")  # bKash (Send Money) shown on Add Funds page
PROVIDER_API_URL = os.environ.get("PROVIDER_API_URL", "")      # e.g. https://provider.com/api/v2
PROVIDER_API_KEY = os.environ.get("PROVIDER_API_KEY", "")      # provider API key
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "atikur")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "atikur123")  # CHANGE THIS before live!
SECRET_KEY     = os.environ.get("SECRET_KEY", "change-me-in-production")
DB_PATH        = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "panel.db"))
# ----------------------------------------

app = Flask(__name__)
app.secret_key = SECRET_KEY

SEED_SERVICES = [
    # (name, platform, price_per_1k BDT, min_qty, max_qty, provider_service_id)
    ("Facebook Followers", "Facebook", 30.0, 100, 100000, ""),
    ("TikTok Followers", "TikTok", 150.0, 100, 100000, ""),
    ("Instagram Followers", "Instagram", 130.0, 100, 100000, ""),
    ("YouTube Subscribers", "YouTube", 1800.0, 100, 20000, ""),
]

# ---------------- DB ----------------
USE_POSTGRES = bool(os.environ.get("DATABASE_URL"))

if USE_POSTGRES:
    import psycopg
    import psycopg.errors
    from psycopg.rows import dict_row

    DUPLICATE_ERRORS = (psycopg.errors.UniqueViolation,)

    class PGConn:
        """Thin wrapper: sqlite-style ? placeholders + dict rows on psycopg."""
        def __init__(self, conn):
            self._c = conn
        def execute(self, sql, params=()):
            cur = self._c.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        def executemany(self, sql, seq):
            cur = self._c.cursor()
            cur.executemany(sql.replace("?", "%s"), seq)
            return cur
        def executescript(self, sql):
            # Run each statement separately: safer on Postgres than one
            # multi-statement string, and harmless on SQLite-style scripts.
            for stmt in sql.split(";"):
                if stmt.strip():
                    self._c.execute(stmt)
        def commit(self):
            self._c.commit()
        def close(self):
            self._c.close()

    def _connect():
        return PGConn(psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row))

    ID_DEF = "SERIAL PRIMARY KEY"
else:
    DUPLICATE_ERRORS = (sqlite3.IntegrityError,)

    def _connect():
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        return db

    ID_DEF = "INTEGER PRIMARY KEY AUTOINCREMENT"

def get_db():
    if "db" not in g:
        g.db = _connect()
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = _connect()
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {ID_DEF},
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS services (
            id {ID_DEF},
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            price_per_1k REAL NOT NULL,
            min_qty INTEGER NOT NULL,
            max_qty INTEGER NOT NULL,
            provider_service_id TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id {ID_DEF},
            user_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            link TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            charge REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_order_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(service_id) REFERENCES services(id)
        );
        CREATE TABLE IF NOT EXISTS payments (
            id {ID_DEF},
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            trxid TEXT NOT NULL,
            sender TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    """)
    if db.execute("SELECT COUNT(*) AS c FROM services").fetchone()["c"] == 0:
        db.executemany(
            "INSERT INTO services (name, platform, price_per_1k, min_qty, max_qty, provider_service_id) VALUES (?,?,?,?,?,?)",
            SEED_SERVICES,
        )
    if db.execute("SELECT COUNT(*) AS c FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()["c"] == 0:
        db.execute(
            "INSERT INTO users (username, password_hash, balance, is_admin, created_at) VALUES (?,?,0,1,?)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), datetime.now().isoformat()),
        )
    db.commit()
    db.close()

# ---------------- Provider API (standard SMM reseller format) ----------------
def provider_configured():
    return bool(PROVIDER_API_URL and PROVIDER_API_KEY)

def provider_add_order(service_id, link, quantity):
    """Returns provider order id or None."""
    if not provider_configured():
        return None
    try:
        r = requests.post(PROVIDER_API_URL, data={
            "key": PROVIDER_API_KEY, "action": "add",
            "service": service_id, "link": link, "quantity": quantity,
        }, timeout=20)
        data = r.json()
        return str(data.get("order")) if data.get("order") else None
    except Exception:
        return None

def provider_order_status(provider_order_id):
    if not provider_configured() or not provider_order_id:
        return None
    try:
        r = requests.post(PROVIDER_API_URL, data={
            "key": PROVIDER_API_KEY, "action": "status", "order": provider_order_id,
        }, timeout=20)
        return r.json().get("status")
    except Exception:
        return None

# ---------------- Auth helpers ----------------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u or not u["is_admin"]:
            flash("Admin access only.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return w

# ---------------- Routes ----------------
@app.route("/")
def index():
    db = get_db()
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY id").fetchall()
    return render_template("index.html", services=services, user=current_user())

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 3 or len(password) < 4:
            flash("Username min 3 chars, password min 4 chars.", "error")
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password_hash, balance, created_at) VALUES (?,?,0,?)",
                    (username, generate_password_hash(password), datetime.now().isoformat()),
                )
                db.commit()
                flash("Account created. Please log in.", "ok")
                return redirect(url_for("login"))
            except DUPLICATE_ERRORS:
                flash("Username already taken.", "error")
    return render_template("register.html", user=current_user())

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        u = db.execute("SELECT * FROM users WHERE username=?",
                       (request.form.get("username", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session["uid"] = u["id"]
            return redirect(url_for("admin" if u["is_admin"] else "dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("login.html", user=current_user())

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    u = current_user()
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY id").fetchall()
    orders = db.execute(
        """SELECT o.*, s.name AS service_name FROM orders o
           JOIN services s ON s.id=o.service_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50""",
        (u["id"],)).fetchall()
    return render_template("dashboard.html", user=u, services=services, orders=orders,
                           provider_ok=provider_configured())

@app.route("/order", methods=["POST"])
@login_required
def place_order():
    db = get_db()
    u = current_user()
    try:
        service_id = int(request.form.get("service_id", 0))
        quantity = int(request.form.get("quantity", 0))
    except ValueError:
        flash("Invalid quantity.", "error")
        return redirect(url_for("dashboard"))
    link = request.form.get("link", "").strip()
    svc = db.execute("SELECT * FROM services WHERE id=? AND active=1", (service_id,)).fetchone()
    if not svc:
        flash("Invalid service.", "error")
        return redirect(url_for("dashboard"))
    if not (link.startswith("http://") or link.startswith("https://")):
        flash("Enter a valid link (http/https).", "error")
        return redirect(url_for("dashboard"))
    if not (svc["min_qty"] <= quantity <= svc["max_qty"]):
        flash(f"Quantity must be {svc['min_qty']}–{svc['max_qty']}.", "error")
        return redirect(url_for("dashboard"))
    charge = round(quantity * svc["price_per_1k"] / 1000, 2)
    if u["balance"] < charge:
        flash(f"Not enough balance. Need ৳{charge}, you have ৳{u['balance']:.2f}. Add funds first.", "error")
        return redirect(url_for("dashboard"))

    db.execute("UPDATE users SET balance=balance-? WHERE id=?", (charge, u["id"]))
    if USE_POSTGRES:
        cur = db.execute(
            "INSERT INTO orders (user_id, service_id, link, quantity, charge, created_at) VALUES (?,?,?,?,?,?) RETURNING id",
            (u["id"], service_id, link, quantity, charge, datetime.now().isoformat()))
        order_id = cur.fetchone()["id"]
    else:
        cur = db.execute(
            "INSERT INTO orders (user_id, service_id, link, quantity, charge, created_at) VALUES (?,?,?,?,?,?)",
            (u["id"], service_id, link, quantity, charge, datetime.now().isoformat()))
        order_id = cur.lastrowid

    # push to provider
    status, poid = "pending", ""
    if provider_configured() and svc["provider_service_id"]:
        poid = provider_add_order(svc["provider_service_id"], link, quantity) or ""
        status = "processing" if poid else "pending"
    db.execute("UPDATE orders SET status=?, provider_order_id=? WHERE id=?", (status, poid, order_id))
    db.commit()
    flash(f"Order #{order_id} placed! Charge ৳{charge}." +
          ("" if poid else " It will start after admin confirmation."), "ok")
    return redirect(url_for("dashboard"))

@app.route("/funds", methods=["GET", "POST"])
@login_required
def funds():
    db = get_db()
    u = current_user()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", 0))
        except ValueError:
            amount = 0
        trxid = request.form.get("trxid", "").strip().upper()
        sender = request.form.get("sender", "").strip()
        if amount < 10:
            flash("Minimum add fund is ৳10.", "error")
        elif not trxid:
            flash("Enter the bKash TrxID.", "error")
        elif not sender:
            flash("Enter the sender bKash number.", "error")
        else:
            db.execute(
                "INSERT INTO payments (user_id, amount, trxid, sender, created_at) VALUES (?,?,?,?,?)",
                (u["id"], amount, trxid, sender, datetime.now().isoformat()))
            db.commit()
            flash("Submitted! Balance will be added after admin approval.", "ok")
            return redirect(url_for("dashboard"))
    my = db.execute("SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],)).fetchall()
    return render_template("funds.html", user=u, bkash=BKASH_NUMBER, payments=my)

@app.route("/admin")
@login_required
@admin_required
def admin():
    db = get_db()
    pending_pay = db.execute(
        """SELECT p.*, u.username FROM payments p JOIN users u ON u.id=p.user_id
           WHERE p.status='pending' ORDER BY p.id DESC""").fetchall()
    recent_pay = db.execute(
        """SELECT p.*, u.username FROM payments p JOIN users u ON u.id=p.user_id
           WHERE p.status!='pending' ORDER BY p.id DESC LIMIT 20""").fetchall()
    orders = db.execute(
        """SELECT o.*, u.username, s.name AS service_name FROM orders o
           JOIN users u ON u.id=o.user_id JOIN services s ON s.id=o.service_id
           ORDER BY o.id DESC LIMIT 50""").fetchall()
    stats = {
        "users": db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "pending_pay": len(pending_pay),
        "revenue": db.execute("SELECT COALESCE(SUM(charge),0) s FROM orders").fetchone()["s"],
    }
    return render_template("admin.html", user=current_user(), pending_pay=pending_pay,
                           recent_pay=recent_pay, orders=orders, stats=stats,
                           provider_ok=provider_configured())

@app.route("/admin/payment/<int:pid>/<action>")
@login_required
@admin_required
def payment_action(pid, action):
    db = get_db()
    p = db.execute("SELECT * FROM payments WHERE id=? AND status='pending'", (pid,)).fetchone()
    if not p:
        flash("Payment not found.", "error")
        return redirect(url_for("admin"))
    if action == "approve":
        db.execute("UPDATE payments SET status='approved' WHERE id=?", (pid,))
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (p["amount"], p["user_id"]))
        flash(f"Approved ৳{p['amount']}.", "ok")
    elif action == "reject":
        db.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        flash("Payment rejected.", "ok")
    db.commit()
    return redirect(url_for("admin"))

@app.route("/admin/sync/<int:oid>")
@login_required
@admin_required
def sync_order(oid):
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if o and o["provider_order_id"]:
        st = provider_order_status(o["provider_order_id"])
        if st:
            db.execute("UPDATE orders SET status=? WHERE id=?", (st, oid))
            db.commit()
            flash(f"Order #{oid} status: {st}", "ok")
        else:
            flash("Could not reach provider.", "error")
    return redirect(url_for("admin"))

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5050, debug=False)
