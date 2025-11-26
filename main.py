import os
import sqlite3
import uuid
import time
from functools import wraps
from datetime import datetime
from threading import Thread
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
import requests

# -------------------- FLASK APP --------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)

DB = "licenses.db"
ADMIN_PASSWORD = "12345"
TG_URL = "https://t.me/your_support_channel"

# -------------------- DATABASE --------------------
def init_db():
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            key TEXT PRIMARY KEY,
            hwid TEXT UNIQUE,
            days_left INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            active INTEGER DEFAULT 0,
            last_tick INTEGER
        )
        """)
        # на случай старой БД без last_tick – пытаемся добавить колонку
        try:
            cur.execute("ALTER TABLE licenses ADD COLUMN last_tick INTEGER")
        except sqlite3.OperationalError:
            # колонка уже есть
            pass
        conn.commit()


def get_license_by_hwid(hwid):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT key, days_left, banned, active, last_tick FROM licenses WHERE hwid=?",
            (hwid,),
        )
        return cur.fetchone()


def get_license_by_key(key):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT key, hwid, days_left, banned, active, last_tick FROM licenses WHERE key=?",
            (key,),
        )
        return cur.fetchone()


def update_license(key, days=None, active=None, banned=None, last_tick=None):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        if days is not None:
            cur.execute("UPDATE licenses SET days_left=? WHERE key=?", (days, key))
        if active is not None:
            cur.execute("UPDATE licenses SET active=? WHERE key=?", (active, key))
        if banned is not None:
            cur.execute("UPDATE licenses SET banned=? WHERE key=?", (banned, key))
        if last_tick is not None:
            cur.execute("UPDATE licenses SET last_tick=? WHERE key=?", (last_tick, key))
        conn.commit()


def sync_license_days(lic):
    """
    При каждом обращении к серверу уменьшает days_left,
    исходя из прошедших полных суток с момента last_tick.
    Возвращает обновлённый кортеж (key, days_left, banned, active, last_tick).
    """
    if not lic:
        return None

    key, days_left, banned, active, last_tick = lic

    # если ключ не активен или забанен – дни не списываем
    if not active or banned:
        return lic

    now = int(time.time())

    if last_tick is None:
        # первый запуск – просто запоминаем текущий момент
        update_license(key, last_tick=now)
        return (key, days_left, banned, active, now)

    elapsed_days = (now - last_tick) // 86400
    if elapsed_days <= 0:
        return lic

    days_left = max(0, days_left - elapsed_days)
    if days_left <= 0:
        active = 0
        new_last_tick = None
    else:
        new_last_tick = now

    update_license(key, days=days_left, active=active, last_tick=new_last_tick)
    return (key, days_left, banned, active, new_last_tick)


def log_action(action, key=None, hwid=None, days=None):
    with open("actions.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()} | {action} | key={key} | hwid={hwid} | days={days}\n")


# -------------------- AUTH DECORATOR --------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# -------------------- ROUTES --------------------
@app.route("/")
def home():
    return "Server is alive!"


# -------------------- LOGIN --------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    LOGIN_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
    <div class="container mt-5">
        <div class="card p-4 mx-auto" style="max-width: 400px;">
            <h3 class="text-center">Admin Login</h3>
            <form method="post">
                <input type="password" name="password" class="form-control mt-3" placeholder="Password">
                <button type="submit" class="btn btn-primary w-100 mt-3">Login</button>
            </form>
            {% if error %}<p class="text-danger mt-2">{{ error }}</p>{% endif %}
        </div>
    </div>
    </body>
    </html>
    """
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect("/admin")
        else:
            error = "Неверный пароль"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect("/login")


# -------------------- LICENSE API --------------------
@app.route("/register", methods=["POST"])
def register_hwid():
    data = request.json
    hwid = data.get("hwid")
    if not hwid:
        return jsonify({"status": "error", "message": "Missing hwid"}), 400

    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT hwid FROM licenses WHERE hwid=?", (hwid,))
        if cur.fetchone():
            return jsonify({"status": "exists", "message": "HWID уже зарегистрирован"}), 200

        new_key = str(uuid.uuid4()).replace("-", "").upper()[:20]
        cur.execute("INSERT INTO licenses (key, hwid, active) VALUES (?, ?, 0)", (new_key, hwid))
        conn.commit()

    log_action("register", key=new_key, hwid=hwid)
    return jsonify({"status": "registered", "key": new_key})


@app.route("/check", methods=["POST"])
def check_license():
    data = request.json
    hwid = data.get("hwid")
    if not hwid:
        return jsonify({"status": "error", "message": "Missing hwid"}), 400

    lic = get_license_by_hwid(hwid)
    # пересчитываем оставшиеся дни при каждом обращении
    lic = sync_license_days(lic)
    if not lic:
        return jsonify({"status": "unregistered"}), 200

    key, days_left, banned, active, _last_tick = lic
    if banned:
        return jsonify({"status": "banned"}), 200
    if not active:
        return jsonify({"status": "inactive"}), 200
    if days_left <= 0:
        return jsonify({"status": "expired"}), 200

    return jsonify({"status": "ok", "days_left": days_left})


@app.route("/activate", methods=["POST"])
@login_required
def activate_license():
    data = request.json
    key = data.get("key")
    days = data.get("days")
    if not key or days is None:
        return jsonify({"status": "error", "message": "Missing key or days"}), 400

    # при активации ставим счётчик дней и текущий момент как точку отсчёта
    now = int(time.time())
    update_license(key, days=days, active=1, banned=0, last_tick=now)
    log_action("activate", key=key, days=days)
    return jsonify({"status": "ok", "days_left": days})


@app.route("/add_days", methods=["POST"])
@login_required
def add_days():
    data = request.json
    key = data.get("key")
    add = data.get("days")
    if not key or add is None:
        return jsonify({"status": "error", "message": "Missing key or days"}), 400

    lic = get_license_by_key(key)
    if not lic:
        return jsonify({"status": "invalid"}), 200
    _, _, days_left, banned, active, last_tick = sync_license_days(lic)
    if banned:
        return jsonify({"status": "banned"}), 200

    new_days = days_left + add
    # обновляем дни и, если нужно, точку отсчёта (оставляем прежний last_tick, чтобы сутки считались от первой активации)
    update_license(key, days=new_days)
    log_action("add_days", key=key, days=add)
    return jsonify({"status": "ok", "days_left": new_days})


@app.route("/ban", methods=["POST"])
@login_required
def ban():
    data = request.json
    key = data.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400

    update_license(key, banned=1)
    log_action("ban", key=key)
    return jsonify({"status": "banned"})


@app.route("/unban", methods=["POST"])
@login_required
def unban():
    data = request.json
    key = data.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400

    lic = get_license_by_key(key)
    if not lic:
        return jsonify({"status": "invalid"}), 200

    update_license(key, banned=0)
    log_action("unban", key=key)
    return jsonify({"status": "ok"})


@app.route("/all")
@login_required
def all_keys():
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, hwid, days_left, banned, active, last_tick FROM licenses")
        data = cur.fetchall()
    formatted = [
        {"key": k, "hwid": h, "days_left": d, "banned": b, "active": a, "last_tick": lt}
        for (k, h, d, b, a, lt) in data
    ]
    return jsonify(formatted)


# -------------------- ADMIN PANEL --------------------
ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TRINITY CODERS</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body>
<div class="container mt-4">
<h2>TRINITY CODERS</h2>

<!-- Уведомления -->
<div id="message" style="margin-bottom:10px;"></div>

<!-- Активировать лицензию -->
<div class="mb-3">
    <input id="act_key" placeholder="Key" class="form-control mb-2">
    <input id="act_days" type="number" placeholder="Days" class="form-control mb-2">
    <button class="btn btn-success" onclick="activate()">Активировать</button>
</div>

<!-- Бан / разбан -->
<div class="mb-3">
    <input id="ban_key" placeholder="Key" class="form-control mb-2">
    <div class="d-flex gap-2">
        <button class="btn btn-danger" onclick="ban()">Забанить</button>
        <button class="btn btn-secondary" onclick="unban()">Разбанить</button>
    </div>
</div>

<!-- Все лицензии -->
<div class="mb-3">
    <button class="btn btn-info" onclick="load_all()">Показать все лицензии</button>
</div>

<table class="table table-striped">
<thead>
<tr>
<th>Key</th>
<th>HWID</th>
<th>Days Left</th>
<th>Active</th>
<th>Banned</th>
</tr>
</thead>
<tbody id="licenses_body"></tbody>
</table>

<script>
function showMessage(msg, type="info") {
    const div = document.getElementById("message");
    div.innerHTML = msg;
    div.className = "alert alert-" + type;
    setTimeout(()=>div.innerHTML="", 3000);
}

function load_all() {
    fetch('/all', { credentials: 'include' }) // обязательно включаем cookie
    .then(r => {
        if (!r.ok) throw new Error("Сервер вернул ошибку");
        return r.json();
    })
    .then(data => {
        const tbody = document.getElementById("licenses_body");
        tbody.innerHTML = "";
        data.forEach(l => {
            const tr = document.createElement("tr");
            if(l.banned) tr.style.backgroundColor = "pink";
            else if(!l.active) tr.style.backgroundColor = "lightyellow";
            tr.innerHTML = `<td>${l.key}</td><td>${l.hwid}</td><td>${l.days_left}</td><td>${l.active}</td><td>${l.banned}</td>`;
            tbody.appendChild(tr);
        });
    })
    .catch(e => showMessage("Ошибка при загрузке лицензий: " + e.message, "danger"));
}

function activate() {
    fetch('/activate', {
        method: 'POST',
        credentials: 'include',  // вот это
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({key: act_key.value, days: Number(act_days.value)})
    })
    .then(r=>r.json())
    .then(d=>{
        if(d.status=="ok") showMessage("Лицензия активирована","success");
        else showMessage(JSON.stringify(d),"danger");
        load_all();
    })
    .catch(e=>showMessage("Ошибка при активации","danger"));
}

function ban() {
    fetch('/ban',{
        method:'POST',
        credentials: 'include',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:ban_key.value})
    })
    .then(r=>r.json())
    .then(d=>{
        if(d.status=="banned") showMessage("Лицензия забанена","warning");
        else showMessage(JSON.stringify(d),"danger");
        load_all();
    })
    .catch(e=>showMessage("Ошибка при бане","danger"));
}

function unban() {
    fetch('/unban',{
        method:'POST',
        credentials: 'include',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:ban_key.value})
    })
    .then(r=>r.json())
    .then(d=>{
        if(d.status=="ok") showMessage("Лицензия разбанена","success");
        else showMessage(JSON.stringify(d),"danger");
        load_all();
    })
    .catch(e=>showMessage("Ошибка при разбане","danger"));
}

// Загружаем таблицу сразу при открытии панели
window.onload = load_all;
</script>
</div>
</body>
</html>
"""

@app.route("/admin")
@login_required
def admin():
    return render_template_string(ADMIN_HTML)


# -------------------- KEEP ALIVE --------------------
def keep_alive():
    """Запускает Flask на отдельном потоке и пингует сам себя каждые 60 секунд"""
    def run():
        app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

    def ping():
        url = "https://a7329ec6-0b42-4112-ad29-f47c6e2dbdea-00-3fg25xrpcyiwx.picard.replit.dev"
        while True:
            try:
                requests.get(url)
                print(f"Pinged {url}")
            except Exception as e:
                print("Ping failed:", e)
            time.sleep(60)

    Thread(target=run).start()
    Thread(target=ping).start()


# -------------------- START SERVER --------------------
if __name__ == "__main__":
    init_db()
    keep_alive()
