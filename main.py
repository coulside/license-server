import os
import sqlite3
import uuid
import time
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string

# -------------------- FLASK APP --------------------
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Путь к базе данных (Windows -> рядом с файлом, Linux -> /tmp)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "licenses.db") if os.name == "nt" else "/tmp/licenses.db"
DB = os.environ.get("DB_PATH", DEFAULT_DB)
ADMIN_PASSWORD = "777"
TG_URL = "https://t.me/your_support_channel"

# Гарантируем, что директория для БД существует (актуально для Render)
db_dir = os.path.dirname(DB) or BASE_DIR
os.makedirs(db_dir, exist_ok=True)

# -------------------- DATABASE --------------------
def init_db():
    print(f"Путь к базе данных: {DB}")  # Логируем путь к базе данных
    if not os.access(os.path.dirname(DB), os.W_OK):
        print("Ошибка: нет прав на запись в директорию базы данных!")
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        # Создаем таблицу, если она не существует
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
        # на случай уже существующей таблицы без last_tick – пытаемся добавить колонку
        try:
            cur.execute("ALTER TABLE licenses ADD COLUMN last_tick INTEGER")
        except sqlite3.OperationalError:
            # колонка уже есть
            pass
        conn.commit()
    print("Инициализация базы данных завершена.")


# Инициализируем базу сразу при импорте модуля (важно для gunicorn/Render)
init_db()

def check_table_exists():
    """Проверка существования таблицы 'licenses' в базе данных"""
    with sqlite3.connect(DB) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT name FROM sqlite_master WHERE type='table' AND name='licenses';
        """)
        return cur.fetchone() is not None

# -------------------- LICENSES --------------------
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
    with open(os.path.join(os.path.dirname(__file__), "actions.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now()} | {action} | key={key} | hwid={hwid} | days={days}\n")

# -------------------- AUTH DECORATOR --------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"status": "error", "message": "Не авторизован"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# -------------------- ROUTES --------------------
def check_db():
    """Проверка наличия таблицы при старте приложения"""
    if not check_table_exists():
        print("Таблица 'licenses' не найдена!")
    else:
        print("Таблица 'licenses' существует.")

# Flask 3.1 удалил before_first_request, поэтому регистрируем безопасно
if hasattr(app, "before_serving"):
    app.before_serving(check_db)
else:
    check_db()

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

    try:
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
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ошибка при добавлении лицензии: {str(e)}"}), 500

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

    try:
        update_license(key, days=days, active=1)
        log_action("activate", key=key, days=days)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ошибка при активации лицензии: {str(e)}"}), 500

@app.route("/add_days", methods=["POST"])
@login_required
def add_days():
    data = request.json
    key = data.get("key")
    add = data.get("days")
    if not key or add is None:
        return jsonify({"status": "error", "message": "Missing key or days"}), 400

    try:
        lic = get_license_by_key(key)
        if not lic:
            return jsonify({"status": "invalid"}), 200

        _, _, days_left, banned, active, _last_tick = sync_license_days(lic)
        if banned:
            return jsonify({"status": "banned"}), 200

        update_license(key, days=days_left + add)
        log_action("add_days", key=key, days=add)
        return jsonify({"status": "ok", "days_left": days_left + add})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ошибка при добавлении дней: {str(e)}"}), 500

@app.route("/ban", methods=["POST"])
@login_required
def ban():
    data = request.json
    key = data.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400

    try:
        update_license(key, banned=1)
        log_action("ban", key=key)
        return jsonify({"status": "banned"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ошибка при бане: {str(e)}"}), 500


@app.route("/unban", methods=["POST"])
@login_required
def unban():
    data = request.json
    key = data.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400

    try:
        lic = get_license_by_key(key)
        if not lic:
            return jsonify({"status": "invalid"}), 200

        update_license(key, banned=0)
        log_action("unban", key=key)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ошибка при разбане: {str(e)}"}), 500

@app.route("/all")
@login_required
def all_keys():
    try:
        with sqlite3.connect(DB) as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, hwid, days_left, banned, active FROM licenses")
            data = cur.fetchall()
        formatted = [{"key": k, "hwid": h, "days_left": d, "banned": b, "active": a} for (k, h, d, b, a) in data]
        return jsonify(formatted)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ошибка при загрузке лицензий: {str(e)}"}), 500

# -------------------- ADMIN PANEL --------------------
ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>TRINITY CODERS</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body style="background-color: #1f1f1f; display: flex; justify-content: center; align-items: center; min-height: 100vh;">
<div style="background-color: #2b2b2b; padding: 20px; border-radius: 10px; width: 90%; max-width: 1200px; color: white;">
<h2>TRINITY CODERS</h2>

<!-- Уведомления -->
<div id="message" style="margin-bottom:10px;"></div>

<!-- Активировать лицензию -->
<div class="mb-3">
    <input id="act_key" placeholder="Key" class="form-control mb-2 bg-dark text-light">
    <input id="act_days" type="number" placeholder="Days" class="form-control mb-2 bg-dark text-light">
    <button class="btn btn-outline-light" onclick="activate()">Активировать</button>
</div>

<!-- Бан / разбан -->
<div class="mb-3">
    <input id="ban_key" placeholder="Key" class="form-control mb-2 bg-dark text-light">
    <div class="d-flex gap-2">
        <button class="btn btn-danger" onclick="ban()">Забанить</button>
        <button class="btn btn-secondary" onclick="unban()">Разбанить</button>
    </div>
</div>

<!-- Все лицензии -->
<div class="mb-3">
    <button class="btn btn-info" onclick="load_all()">Показать все лицензии</button>
</div>

<table class="table table-dark table-striped">
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
    div.className = "alert alert-" + type + " text-light";
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

# -------------------- START SERVER --------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
