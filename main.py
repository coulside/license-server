import os
import sqlite3
import uuid
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string

# -------------------- FLASK APP --------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)

# База хранится в рабочей директории Render
DB = os.path.join(os.path.dirname(__file__), "licenses.db")
ADMIN_PASSWORD = "777"
TG_URL = "https://t.me/your_support_channel"

# -------------------- DATABASE --------------------
def init_db():
    if not os.access(os.path.dirname(DB), os.W_OK):
        print("Ошибка: нет прав на запись в директорию базы данных!")
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            key TEXT PRIMARY KEY,
            hwid TEXT UNIQUE,
            days_left INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            active INTEGER DEFAULT 0
        )
        """)
        conn.commit()

def get_license_by_hwid(hwid):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, days_left, banned, active FROM licenses WHERE hwid=?", (hwid,))
        return cur.fetchone()

def get_license_by_key(key):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, hwid, days_left, banned, active FROM licenses WHERE key=?", (key,))
        return cur.fetchone()

def update_license(key, days=None, active=None, banned=None):
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        if days is not None:
            cur.execute("UPDATE licenses SET days_left=? WHERE key=?", (days, key))
        if active is not None:
            cur.execute("UPDATE licenses SET active=? WHERE key=?", (active, key))
        if banned is not None:
            cur.execute("UPDATE licenses SET banned=? WHERE key=?", (banned, key))
        conn.commit()

def log_action(action, key=None, hwid=None, days=None):
    with open(os.path.join(os.path.dirname(__file__), "actions.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()} | {action} | key={key} | hwid={hwid} | days={days}\n")

# -------------------- AUTH DECORATOR --------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            # Если AJAX-запрос, вернуть JSON
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "error", "message": "Не авторизован"}), 401
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
        <title>Admin Login</title>
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
    if not lic:
        return jsonify({"status": "unregistered"}), 200

    key, days_left, banned, active = lic
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

    update_license(key, days=days, active=1)
    log_action("activate", key=key, days=days)
    return jsonify({"status": "ok"})

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

    _, _, days_left, banned, active = lic
    if banned:
        return jsonify({"status": "banned"}), 200

    update_license(key, days=days_left + add)
    log_action("add_days", key=key, days=add)
    return jsonify({"status": "ok", "days_left": days_left + add})

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

@app.route("/all")
@login_required
def all_keys():
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, hwid, days_left, banned, active FROM licenses")
        data = cur.fetchall()
    formatted = [{"key": k, "hwid": h, "days_left": d, "banned": b, "active": a} for (k, h, d, b, a) in data]
    return jsonify(formatted)

# -------------------- ADMIN PANEL --------------------
ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TRINITY CODERS</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .inactive { background-color: lightyellow; }
        .banned { background-color: pink; }
        .active { background-color: lightgreen; }
    </style>
</head>
<body>
<div class="container mt-4">
<h2>TRINITY CODERS</h2>
<div id="message" style="margin-bottom:10px;"></div>

<div class="mb-3">
    <input type="text" id="new_hwid" class="form-control mb-2" placeholder="Введите HWID">
    <button class="btn btn-primary" onclick="add_license()">Добавить лицензию</button>
</div>

<table class="table table-striped">
<thead>
<tr>
<th>Key</th>
<th>HWID</th>
<th>Days Left</th>
<th>Status</th>
<th>Действия</th>
</tr>
</thead>
<tbody id="licenses_body"></tbody>
</table>

<script>
document.addEventListener("DOMContentLoaded", load_all);

function showMessage(msg, type="info") {
    const div = document.getElementById("message");
    div.innerHTML = msg;
    div.className = "alert alert-" + type;
    setTimeout(()=>div.innerHTML="", 3000);
}

function load_all() {
    fetch('/all', { credentials: 'include' })
    .then(r => r.json())
    .then(data => {
        if(data.status === "error"){ showMessage(data.message,"danger"); return; }
        const tbody = document.getElementById("licenses_body");
        tbody.innerHTML = "";
        if(!data || data.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center">Лицензий нет</td></tr>';
            return;
        }
        data.forEach(l => {
            const tr = document.createElement("tr");
            if(l.banned) tr.className = "banned";
            else if(!l.active) tr.className = "inactive";
            else tr.className = "active";
            const statusText = l.banned ? "Забанена" : (l.active ? "Активна" : "Неактивна");
            tr.innerHTML = `
                <td>${l.key}</td>
                <td>${l.hwid || ""}</td>
                <td>${l.days_left}</td>
                <td>${statusText}</td>
                <td>
                    <input type="number" min="1" placeholder="Days" style="width:60px;" id="days_${l.key}">
                    <button class="btn btn-success btn-sm" onclick="activate('${l.key}')">Активировать</button>
                    <button class="btn btn-danger btn-sm" onclick="ban('${l.key}')">Забанить</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    })
    .catch(e => showMessage("Ошибка при загрузке лицензий: " + e.message, "danger"));
}

function add_license() {
    const hwid = document.getElementById("new_hwid").value.trim();
    if(!hwid) { showMessage("Введите HWID", "warning"); return; }
    fetch('/register', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({hwid: hwid})
    })
    .then(r => r.json())
    .then(d => {
        if(d.status === "registered") showMessage("Лицензия добавлена: " + d.key, "success");
        else showMessage(JSON.stringify(d),"danger");
        load_all();
    })
    .catch(e => showMessage("Ошибка при добавлении лицензии","danger"));
}

function activate(key){
    const days = Number(document.getElementById(`days_${key}`).value);
    if(!days || days<=0){ showMessage("Введите корректное количество дней","warning"); return; }
    fetch('/activate',{
        method:'POST',
        credentials:'include',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:key, days:days})
    })
    .then(r=>r.json())
    .then(d=>{ showMessage(d.status=="ok"?"Лицензия активирована":"Ошибка", d.status=="ok"?"success":"danger"); load_all(); })
    .catch(e=>showMessage("Ошибка при активации","danger"));
}

function ban(key){
    fetch('/ban',{
        method:'POST',
        credentials:'include',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({key:key})
    })
    .then(r=>r.json())
    .then(d=>{ showMessage(d.status=="banned"?"Лицензия забанена":"Ошибка", d.status=="banned"?"warning":"danger"); load_all(); })
    .catch(e=>showMessage("Ошибка при бане","danger"));
}
</script>
</div>
</body>
</html>
"""

@app.route("/admin")
@login_required
def admin():
    return render_template_string(ADMIN_HTML)

# -------------------- START SERVER --------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
