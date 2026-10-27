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
    # (name, platform, price_per_1k BDT, min_qty, max_qty, provider_service_id, description)
    # ONLY the original 4 services. The owner adds further services manually
    # from the admin panel; nothing else is seeded automatically.
    # Descriptions are neutral only — no delivery-time or refill promises.
    ("Facebook Followers", "Facebook", 30.0, 100, 100000, "", "Followers for your Facebook profile or page."),
    ("TikTok Followers", "TikTok", 150.0, 100, 100000, "", "Followers for your TikTok account."),
    ("Instagram Followers", "Instagram", 130.0, 100, 100000, "", "Followers for your Instagram account."),
    ("YouTube Subscribers", "YouTube", 1800.0, 100, 20000, "", "Subscribers for your YouTube channel."),
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
            created_at TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS services (
            id {ID_DEF},
            name TEXT NOT NULL,
            platform TEXT NOT NULL,
            price_per_1k REAL NOT NULL,
            min_qty INTEGER NOT NULL,
            max_qty INTEGER NOT NULL,
            provider_service_id TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            markup_percent REAL,
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
        CREATE TABLE IF NOT EXISTS tickets (
            id {ID_DEF},
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            order_id INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL DEFAULT 'Other',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS ticket_replies (
            id {ID_DEF},
            ticket_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticket_id) REFERENCES tickets(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );
    """)
    # Migrations for databases created before these columns existed.
    # SQLite has no ADD COLUMN IF NOT EXISTS, so ignore the duplicate-column error.
    if USE_POSTGRES:
        db.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS markup_percent REAL")
        db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Other'")
    else:
        for coldef in ("description TEXT NOT NULL DEFAULT ''", "markup_percent REAL"):
            try:
                db.execute(f"ALTER TABLE services ADD COLUMN {coldef}")
            except sqlite3.OperationalError:
                pass
        for coldef in ("email TEXT NOT NULL DEFAULT ''", "phone TEXT NOT NULL DEFAULT ''"):
            try:
                db.execute(f"ALTER TABLE users ADD COLUMN {coldef}")
            except sqlite3.OperationalError:
                pass
        try:
            db.execute("ALTER TABLE tickets ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'")
        except sqlite3.OperationalError:
            pass
    # Seed: insert any catalog service missing by name, and backfill empty
    # descriptions. Matching by name keeps this idempotent on repeat runs.
    for name, platform, price, mn, mx, psid, desc in SEED_SERVICES:
        row = db.execute("SELECT id, description FROM services WHERE name=?", (name,)).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO services (name, platform, price_per_1k, min_qty, max_qty, provider_service_id, description) VALUES (?,?,?,?,?,?,?)",
                (name, platform, price, mn, mx, psid, desc),
            )
        elif not row["description"]:
            db.execute("UPDATE services SET description=? WHERE id=?", (desc, row["id"]))
    # One-time defensive catalog revert (runs once, gated by a settings flag):
    # the catalog used to auto-seed 17 extra services; the owner now adds
    # services manually, so hide those extras again. NEVER delete anything,
    # and NEVER touch a service that already has orders — only deactivate
    # active, order-free services that are not in the seed list. Because this
    # runs only once, services the admin adds afterwards are never affected.
    seed_names = [s[0] for s in SEED_SERVICES]
    flag = db.execute("SELECT value FROM settings WHERE key='catalog_revert_done'").fetchone()
    if not flag:
        placeholders = ",".join("?" * len(seed_names))
        db.execute(
            f"""UPDATE services SET active=0 WHERE active=1 AND name NOT IN ({placeholders})
                AND id NOT IN (SELECT DISTINCT service_id FROM orders)""",
            tuple(seed_names),
        )
        db.execute("INSERT INTO settings (key, value) VALUES ('catalog_revert_done', '1')")
    if db.execute("SELECT COUNT(*) AS c FROM users WHERE username=?", (ADMIN_USERNAME,)).fetchone()["c"] == 0:
        db.execute(
            "INSERT INTO users (username, password_hash, balance, is_admin, created_at) VALUES (?,?,0,1,?)",
            (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD), datetime.now().isoformat()),
        )
    db.commit()
    db.close()

# ---------------- Settings (DB key/value store) ----------------
# Provider details + default markup are stored here for LATER activation.
# They are intentionally NOT wired into the order flow yet: orders stay
# pending for manual admin handling exactly as today. A future activation
# step will decide how these values drive provider calls and pricing.
def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(db, key, value):
    if db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
        db.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
    else:
        db.execute("INSERT INTO settings (key, value) VALUES (?,?)", (key, value))

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
    # Honest, live figures only: counts computed from this panel's own catalog,
    # and the platform grid lists only the services that actually exist.
    platforms = sorted({s["platform"] for s in services})
    platform_services = {}
    for s in services:
        platform_services.setdefault(s["platform"], []).append(s["name"])
    stats = {"services": len(services), "platforms": len(platforms)}
    return render_template("index.html", services=services, user=current_user(),
                           stats=stats, platforms=platforms,
                           platform_services=platform_services)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        if len(username) < 3 or len(password) < 4:
            flash("Username min 3 chars, password min 4 chars.", "error")
        elif "@" not in email or "." not in email.split("@")[-1]:
            flash("Please enter a valid email address.", "error")
        elif len(phone) < 6:
            flash("Please enter a valid phone number.", "error")
        else:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, password_hash, balance, created_at, email, phone) VALUES (?,?,0,?,?,?)",
                    (username, generate_password_hash(password), datetime.now().isoformat(), email, phone),
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
    platforms = sorted({s["platform"] for s in services})
    stats = {
        "spent": db.execute("SELECT COALESCE(SUM(charge),0) s FROM orders WHERE user_id=?", (u["id"],)).fetchone()["s"],
        "orders": db.execute("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],)).fetchone()["c"],
    }
    return render_template("dashboard.html", user=u, services=services, stats=stats,
                           platforms=platforms, provider_ok=provider_configured())

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

@app.route("/services")
@login_required
def services_page():
    db = get_db()
    q = request.args.get("q", "").strip()
    all_services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY platform, id").fetchall()
    if q:
        ql = q.lower()
        all_services = [s for s in all_services if ql in s["name"].lower() or ql in s["platform"].lower()]
    grouped = {}
    for s in all_services:
        grouped.setdefault(s["platform"], []).append(s)
    return render_template("services.html", user=current_user(), grouped=grouped, q=q)

@app.route("/mass-order", methods=["GET", "POST"])
@login_required
def mass_order():
    db = get_db()
    u = current_user()
    if request.method == "POST":
        raw = request.form.get("orders", "")
        lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
        parsed, errors, total = [], [], 0.0
        for i, ln in enumerate(lines, 1):
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) != 3:
                errors.append(f"Line {i}: use service_id | link | quantity.")
                continue
            try:
                sid, qty = int(parts[0]), int(parts[2])
            except ValueError:
                errors.append(f"Line {i}: service id and quantity must be numbers.")
                continue
            link = parts[1]
            svc = db.execute("SELECT * FROM services WHERE id=? AND active=1", (sid,)).fetchone()
            if not svc:
                errors.append(f"Line {i}: unknown or inactive service id {sid}.")
                continue
            if not (link.startswith("http://") or link.startswith("https://")):
                errors.append(f"Line {i}: link must start with http/https.")
                continue
            if not (svc["min_qty"] <= qty <= svc["max_qty"]):
                errors.append(f"Line {i}: quantity must be {svc['min_qty']}–{svc['max_qty']} for {svc['name']}.")
                continue
            charge = round(qty * svc["price_per_1k"] / 1000, 2)
            parsed.append((sid, link, qty, charge))
            total += charge
        if not lines:
            errors.append("Paste at least one order line.")
        if errors:
            for e in errors:
                flash(e, "error")
        elif total > u["balance"]:
            flash(f"Not enough balance. Total ৳{total:.2f}, you have ৳{u['balance']:.2f}.", "error")
        else:
            # Mass orders are created pending for manual handling, exactly
            # like single orders without a connected provider. Nothing is
            # sent to any provider here; validation above is all-or-nothing
            # so a bad line never creates a partial batch.
            db.execute("UPDATE users SET balance=balance-? WHERE id=?", (total, u["id"]))
            now = datetime.now().isoformat()
            for sid, link, qty, charge in parsed:
                db.execute("INSERT INTO orders (user_id, service_id, link, quantity, charge, created_at) VALUES (?,?,?,?,?,?)",
                           (u["id"], sid, link, qty, charge, now))
            db.commit()
            flash(f"{len(parsed)} orders placed! Total charge ৳{total:.2f}. They will start after admin confirmation.", "ok")
            return redirect(url_for("orders_page"))
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY id").fetchall()
    return render_template("mass_order.html", user=u, services=services)

@app.route("/orders")
@login_required
def orders_page():
    db = get_db()
    u = current_user()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "all")
    orders = db.execute(
        """SELECT o.*, s.name AS service_name FROM orders o
           JOIN services s ON s.id=o.service_id WHERE o.user_id=? ORDER BY o.id DESC""",
        (u["id"],)).fetchall()
    if status != "all":
        # "In progress" and "Processing" are the same underlying status in
        # this panel, so both pills filter to it.
        want = "processing" if status in ("processing", "in_progress") else status
        orders = [o for o in orders if o["status"] == want]
    if q:
        ql = q.lower()
        orders = [o for o in orders if ql in o["link"].lower() or ql in o["service_name"].lower() or ql == str(o["id"])]
    return render_template("orders.html", user=u, orders=orders, q=q, status=status)

@app.route("/account")
@login_required
def account():
    db = get_db()
    u = current_user()
    spent = db.execute("SELECT COALESCE(SUM(charge),0) s FROM orders WHERE user_id=?", (u["id"],)).fetchone()["s"]
    count = db.execute("SELECT COUNT(*) c FROM orders WHERE user_id=?", (u["id"],)).fetchone()["c"]
    return render_template("account.html", user=u, spent=spent, order_count=count)

@app.route("/account/change-password", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    u = current_user()
    cur = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    conf = request.form.get("confirm_password", "")
    if not check_password_hash(u["password_hash"], cur):
        flash("Current password is incorrect.", "error")
    elif len(new) < 4:
        flash("New password must be at least 4 characters.", "error")
    elif new != conf:
        flash("New passwords do not match.", "error")
    else:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), u["id"]))
        db.commit()
        flash("Password changed successfully.", "ok")
    return redirect(url_for("account"))

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

@app.route("/tickets", methods=["GET", "POST"])
@login_required
def tickets():
    db = get_db()
    u = current_user()
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()
        category = request.form.get("category", "Other").strip()
        if category not in ("Refill", "Cancel", "Speed up", "Other"):
            category = "Other"
        try:
            order_id = int(request.form.get("order_id", 0) or 0)
        except ValueError:
            order_id = 0
        if order_id:
            # Only allow linking the user's own order; otherwise drop the link.
            own = db.execute("SELECT id FROM orders WHERE id=? AND user_id=?",
                             (order_id, u["id"])).fetchone()
            if not own:
                order_id = 0
        if not subject or not message:
            flash("Subject and message are required.", "error")
        else:
            db.execute(
                "INSERT INTO tickets (user_id, subject, message, order_id, category, created_at) VALUES (?,?,?,?,?,?)",
                (u["id"], subject, message, order_id, category, datetime.now().isoformat()))
            db.commit()
            flash("Ticket submitted. Admin will reply here.", "ok")
            return redirect(url_for("tickets"))
    my = db.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    return render_template("tickets.html", user=u, tickets=my)

@app.route("/tickets/<int:tid>", methods=["GET", "POST"])
@login_required
def ticket_detail(tid):
    db = get_db()
    u = current_user()
    t = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t or (t["user_id"] != u["id"] and not u["is_admin"]):
        flash("Ticket not found.", "error")
        return redirect(url_for("tickets" if not u["is_admin"] else "admin"))
    if request.method == "POST":
        msg = request.form.get("message", "").strip()
        if msg:
            db.execute(
                "INSERT INTO ticket_replies (ticket_id, user_id, message, created_at) VALUES (?,?,?,?)",
                (tid, u["id"], msg, datetime.now().isoformat()))
            # An admin reply reopens a closed ticket so the user sees it.
            if u["is_admin"] and t["status"] == "closed":
                db.execute("UPDATE tickets SET status='open' WHERE id=?", (tid,))
            db.commit()
            return redirect(url_for("ticket_detail", tid=tid))
        flash("Reply cannot be empty.", "error")
    replies = db.execute(
        """SELECT r.*, u2.username FROM ticket_replies r
           JOIN users u2 ON u2.id=r.user_id WHERE r.ticket_id=? ORDER BY r.id""",
        (tid,)).fetchall()
    owner = db.execute("SELECT username FROM users WHERE id=?", (t["user_id"],)).fetchone()
    return render_template("ticket_detail.html", user=u, ticket=t, replies=replies,
                           owner_name=owner["username"] if owner else "?")

@app.route("/tickets/<int:tid>/close", methods=["POST"])
@login_required
@admin_required
def ticket_close(tid):
    db = get_db()
    db.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
    db.commit()
    flash(f"Ticket #{tid} closed.", "ok")
    return redirect(url_for("admin"))

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
    tickets = db.execute(
        """SELECT t.*, u.username FROM tickets t JOIN users u ON u.id=t.user_id
           ORDER BY t.id DESC LIMIT 50""").fetchall()
    all_services = db.execute("SELECT * FROM services ORDER BY id").fetchall()
    all_users = db.execute("SELECT id, username, email, phone, balance, created_at FROM users ORDER BY id").fetchall()
    # Provider details saved in the DB (for later activation only). The API
    # key value is NEVER passed to the template — only whether one is saved.
    settings = {
        "provider_api_url": get_setting(db, "provider_api_url"),
        "default_markup_percent": get_setting(db, "default_markup_percent"),
        "provider_api_key_saved": bool(get_setting(db, "provider_api_key")),
        "gateway_name": get_setting(db, "gateway_name"),
        "gateway_merchant_id": get_setting(db, "gateway_merchant_id"),
        "gateway_public_key": get_setting(db, "gateway_public_key"),
        "gateway_private_key_saved": bool(get_setting(db, "gateway_private_key")),
        "gateway_sandbox": get_setting(db, "gateway_sandbox"),
        "gateway_enabled": get_setting(db, "gateway_enabled"),
    }
    stats = {
        "users": db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "pending_pay": len(pending_pay),
        "revenue": db.execute("SELECT COALESCE(SUM(charge),0) s FROM orders").fetchone()["s"],
        "open_tickets": db.execute("SELECT COUNT(*) c FROM tickets WHERE status='open'").fetchone()["c"],
    }
    return render_template("admin.html", user=current_user(), pending_pay=pending_pay,
                           recent_pay=recent_pay, orders=orders, tickets=tickets, stats=stats,
                           provider_ok=provider_configured(), services=all_services,
                           users=all_users, settings=settings)

def _parse_service_form(form):
    """Validate admin service fields. Returns (values, error)."""
    name = form.get("name", "").strip()
    platform = form.get("platform", "").strip()
    description = form.get("description", "").strip()
    psid = form.get("provider_service_id", "").strip()
    try:
        price = float(form.get("price_per_1k", 0))
        mn = int(form.get("min_qty", 0))
        mx = int(form.get("max_qty", 0))
        markup_raw = (form.get("markup_percent", "") or "").strip()
        markup = float(markup_raw) if markup_raw else None
    except ValueError:
        return None, "Price, quantities and markup must be numbers."
    if price <= 0:
        return None, "Price per 1000 must be greater than 0."
    if mn < 1 or mx < mn:
        return None, "Min must be at least 1 and max must be at least min."
    if markup is not None and markup < 0:
        return None, "Markup percent cannot be negative."
    return {"name": name, "platform": platform, "price": price, "mn": mn,
            "mx": mx, "psid": psid, "description": description,
            "markup": markup}, None

@app.route("/admin/service/add", methods=["POST"])
@login_required
@admin_required
def service_add():
    db = get_db()
    vals, err = _parse_service_form(request.form)
    if err:
        flash(err, "error")
    elif not vals["name"] or not vals["platform"]:
        flash("Service name and platform are required.", "error")
    elif db.execute("SELECT id FROM services WHERE name=?", (vals["name"],)).fetchone():
        flash("A service with that name already exists — reactivate it instead of adding a duplicate.", "error")
    else:
        db.execute(
            """INSERT INTO services (name, platform, price_per_1k, min_qty, max_qty,
               provider_service_id, description, markup_percent, active)
               VALUES (?,?,?,?,?,?,?,?,1)""",
            (vals["name"], vals["platform"], vals["price"], vals["mn"], vals["mx"],
             vals["psid"], vals["description"], vals["markup"]))
        db.commit()
        flash(f"Service '{vals['name']}' added.", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/service/<int:sid>/edit", methods=["POST"])
@login_required
@admin_required
def service_edit(sid):
    db = get_db()
    vals, err = _parse_service_form(request.form)
    if err:
        flash(err, "error")
    elif not db.execute("SELECT id FROM services WHERE id=?", (sid,)).fetchone():
        flash("Service not found.", "error")
    else:
        # Name/platform are fixed at creation so seeded and order history
        # stay unambiguous; price, limits, provider id, markup and
        # description are editable.
        db.execute(
            """UPDATE services SET price_per_1k=?, min_qty=?, max_qty=?,
               provider_service_id=?, description=?, markup_percent=? WHERE id=?""",
            (vals["price"], vals["mn"], vals["mx"], vals["psid"],
             vals["description"], vals["markup"], sid))
        db.commit()
        flash("Service updated.", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/service/<int:sid>/deactivate", methods=["POST"])
@login_required
@admin_required
def service_deactivate(sid):
    db = get_db()
    # Deactivation only hides the service from the catalog; it is never a
    # delete, so order history referencing it stays intact.
    db.execute("UPDATE services SET active=0 WHERE id=?", (sid,))
    db.commit()
    flash("Service deactivated (hidden from the catalog, not deleted).", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/service/<int:sid>/activate", methods=["POST"])
@login_required
@admin_required
def service_activate(sid):
    db = get_db()
    db.execute("UPDATE services SET active=1 WHERE id=?", (sid,))
    db.commit()
    flash("Service reactivated.", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/settings", methods=["POST"])
@login_required
@admin_required
def admin_settings():
    db = get_db()
    set_setting(db, "provider_api_url", request.form.get("provider_api_url", "").strip())
    # Blank key field = keep the saved key; never render or echo the key.
    new_key = request.form.get("provider_api_key", "")
    if new_key:
        set_setting(db, "provider_api_key", new_key)
    markup_raw = (request.form.get("default_markup_percent", "") or "").strip()
    try:
        if markup_raw:
            if float(markup_raw) < 0:
                raise ValueError
            set_setting(db, "default_markup_percent", markup_raw)
        else:
            set_setting(db, "default_markup_percent", "")
    except ValueError:
        flash("Markup percent must be a non-negative number.", "error")
        db.commit()
        return redirect(url_for("admin"))
    db.commit()
    flash("Provider settings saved (for later activation — orders are still handled manually).", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/gateway_settings", methods=["POST"])
@login_required
@admin_required
def gateway_settings():
    # Payment-gateway option the owner fills himself later. Saving these
    # does NOT change checkout: manual bKash funds stay the live method
    # until a gateway is actually wired up and activated.
    db = get_db()
    set_setting(db, "gateway_name", request.form.get("gateway_name", "").strip())
    set_setting(db, "gateway_merchant_id", request.form.get("gateway_merchant_id", "").strip())
    set_setting(db, "gateway_public_key", request.form.get("gateway_public_key", "").strip())
    # Blank private-key field = keep the saved key; never render or echo it.
    new_priv = request.form.get("gateway_private_key", "")
    if new_priv:
        set_setting(db, "gateway_private_key", new_priv)
    set_setting(db, "gateway_sandbox", "1" if request.form.get("gateway_sandbox") else "")
    set_setting(db, "gateway_enabled", "1" if request.form.get("gateway_enabled") else "")
    db.commit()
    flash("Payment gateway option saved (manual funds still live until a gateway is activated).", "ok")
    return redirect(url_for("admin"))


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
