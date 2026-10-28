#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sistema de Bordado v3
Flujo: Recepcion -> Bordados -> Asignacion (1..N operadores) -> Registro productividad -> Trazabilidad
"""
from flask import Flask, request, redirect, url_for, session, render_template_string
from functools import wraps
from datetime import datetime, timedelta, time as dtime
import os, hashlib, json

app = Flask(__name__)
_INIT_DONE = False
APP_VERSION = "2026-10-28-bordado-v3"
app.secret_key = os.environ.get("SECRET_KEY", "clave_secreta_bordado_v3_2026")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3
    from pathlib import Path as _P
    DB_PATH = _P(__file__).parent / "bordado.db"

MAQUINAS = [
    {"id": "2c", "nombre": "Bordadora 2 cabezas", "cabezas": 2},
    {"id": "4c", "nombre": "Bordadora 4 cabezas", "cabezas": 4},
    {"id": "6c", "nombre": "Bordadora 6 cabezas", "cabezas": 6},
]
MAQ_BY_ID = {m["id"]: m for m in MAQUINAS}
ESTADOS_TRABAJO = ["Recibido", "Asignado", "En proceso", "Terminado", "Cancelado"]
ESTADOS_ASIG = ["Pendiente", "En proceso", "Terminado"]

def get_db():
    if USE_POSTGRES:
        try:
            return psycopg2.connect(
                DATABASE_URL, connect_timeout=8,
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
        except Exception as e:
            raise RuntimeError("No se pudo conectar a PostgreSQL. Revisa DATABASE_URL. %s" % e)
    conn = sqlite3.connect(str(DB_PATH), timeout=8)
    conn.row_factory = sqlite3.Row
    return conn

def dcur(conn):
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()

def ph():
    return "%s" if USE_POSTGRES else "?"

def h(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def as_dict(row):
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        try:
            return dict(row)
        except Exception:
            return {}

def as_dicts(rows):
    return [as_dict(r) for r in (rows or [])]


def init_db():
    global _INIT_DONE
    if _INIT_DONE:
        return
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, nombre TEXT, rol TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS trabajos (
            id SERIAL PRIMARY KEY,
            vale TEXT DEFAULT '', proyecto TEXT DEFAULT '', cliente TEXT DEFAULT '',
            estado TEXT DEFAULT 'Recibido',
            recibido_de TEXT DEFAULT '', recibido_fecha TEXT DEFAULT '', recibido_por TEXT DEFAULT '',
            piezas_recibidas INT DEFAULT 0, obs_recepcion TEXT DEFAULT '',
            observaciones TEXT DEFAULT '',
            creado_por TEXT, creado_en TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS bordados (
            id SERIAL PRIMARY KEY,
            trabajo_id INT REFERENCES trabajos(id) ON DELETE CASCADE,
            nombre TEXT NOT NULL,
            puntadas INT DEFAULT 0,
            minutos_proyectados REAL DEFAULT 0,
            modelo TEXT DEFAULT '', color TEXT DEFAULT '',
            observaciones TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS asignaciones (
            id SERIAL PRIMARY KEY,
            trabajo_id INT REFERENCES trabajos(id) ON DELETE CASCADE,
            bordado_id INT,
            operador TEXT NOT NULL,
            maquina_id TEXT DEFAULT '',
            piezas_objetivo INT DEFAULT 0,
            estado TEXT DEFAULT 'Pendiente',
            fecha_asignacion TEXT,
            asignado_por TEXT,
            observaciones TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS registros (
            id SERIAL PRIMARY KEY,
            trabajo_id INT REFERENCES trabajos(id) ON DELETE CASCADE,
            asignacion_id INT,
            bordado_id INT,
            operador TEXT NOT NULL,
            maquina_id TEXT NOT NULL,
            piezas INT DEFAULT 0,
            puntadas_unit INT DEFAULT 0,
            minutos_proyectados REAL DEFAULT 0,
            fecha_inicio TEXT, hora_inicio TEXT,
            fecha_fin TEXT, hora_fin TEXT,
            incidencia TEXT DEFAULT '',
            observaciones TEXT DEFAULT '',
            registrado_por TEXT, creado_en TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS historial (
            id SERIAL PRIMARY KEY,
            trabajo_id INT,
            evento TEXT,
            detalle TEXT,
            usuario TEXT,
            fecha TEXT)""")
    else:
        cur.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, nombre TEXT, rol TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS trabajos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vale TEXT DEFAULT '', proyecto TEXT DEFAULT '', cliente TEXT DEFAULT '',
            estado TEXT DEFAULT 'Recibido',
            recibido_de TEXT DEFAULT '', recibido_fecha TEXT DEFAULT '', recibido_por TEXT DEFAULT '',
            piezas_recibidas INTEGER DEFAULT 0, obs_recepcion TEXT DEFAULT '',
            observaciones TEXT DEFAULT '',
            creado_por TEXT, creado_en TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS bordados (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trabajo_id INTEGER,
            nombre TEXT NOT NULL, puntadas INTEGER DEFAULT 0,
            minutos_proyectados REAL DEFAULT 0,
            modelo TEXT DEFAULT '', color TEXT DEFAULT '', observaciones TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS asignaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trabajo_id INTEGER, bordado_id INTEGER,
            operador TEXT NOT NULL, maquina_id TEXT DEFAULT '',
            piezas_objetivo INTEGER DEFAULT 0, estado TEXT DEFAULT 'Pendiente',
            fecha_asignacion TEXT, asignado_por TEXT, observaciones TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trabajo_id INTEGER, asignacion_id INTEGER, bordado_id INTEGER,
            operador TEXT NOT NULL, maquina_id TEXT NOT NULL,
            piezas INTEGER DEFAULT 0, puntadas_unit INTEGER DEFAULT 0,
            minutos_proyectados REAL DEFAULT 0,
            fecha_inicio TEXT, hora_inicio TEXT, fecha_fin TEXT, hora_fin TEXT,
            incidencia TEXT DEFAULT '', observaciones TEXT DEFAULT '',
            registrado_por TEXT, creado_en TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS historial (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trabajo_id INTEGER,
            evento TEXT, detalle TEXT, usuario TEXT, fecha TEXT)""")
    for u, p, n, r in [
        ("admin", h("admin123"), "Administrador", "admin"),
        ("gerente", h("gerente123"), "Gerente", "gerente"),
        ("operador", h("operador123"), "Operador", "operador"),
    ]:
        try:
            if USE_POSTGRES:
                cur.execute(
                    "INSERT INTO usuarios (username,password_hash,nombre,rol) VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
                    (u, p, n, r))
            else:
                cur.execute(
                    "INSERT OR IGNORE INTO usuarios (username,password_hash,nombre,rol) VALUES (?,?,?,?)",
                    (u, p, n, r))
        except Exception:
            pass
    try:
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass
    _INIT_DONE = True

@app.before_request
def _ensure_db():
    try:
        init_db()
    except Exception as e:
        print("init_db ERROR:", e)

def log_evento(cur, trabajo_id, evento, detalle=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = session.get("username") or ""
    try:
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO historial (trabajo_id,evento,detalle,usuario,fecha) VALUES (%s,%s,%s,%s,%s)",
                (trabajo_id, evento, detalle, user, now))
        else:
            cur.execute(
                "INSERT INTO historial (trabajo_id,evento,detalle,usuario,fecha) VALUES (?,?,?,?,?)",
                (trabajo_id, evento, detalle, user, now))
    except Exception as e:
        print("log_evento", e)


def login_required(f):
    @wraps(f)
    def d(*a, **k):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*a, **k)
    return d

def can_edit():
    return session.get("rol") in ("admin", "gerente", "operador")

def is_admin_or_gerente():
    return session.get("rol") in ("admin", "gerente")

def edit_required(f):
    @wraps(f)
    def d(*a, **k):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if not can_edit():
            return page("Sin permiso", '<div class="alert alert-err">Sin permiso.</div>', session)
        return f(*a, **k)
    return d

def parse_dt(fecha, hora):
    if not fecha:
        return None
    hh = (hora or "00:00").strip()
    if len(hh) == 5:
        hh += ":00"
    try:
        return datetime.strptime(fecha[:10] + " " + hh[:8], "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime(fecha[:10] + " " + hh[:5], "%Y-%m-%d %H:%M")
        except Exception:
            return None

def jornada_del_dia(d):
    if d.weekday() > 5:
        return None
    if d.weekday() == 0:
        ini, fin = dtime(8, 0), dtime(18, 0)
    else:
        ini, fin = dtime(8, 0), dtime(17, 30)
    return (
        datetime.combine(d, ini), datetime.combine(d, fin),
        datetime.combine(d, dtime(14, 0)), datetime.combine(d, dtime(15, 0)))

def minutos_en_jornada(t0, t1):
    if not t0 or not t1 or t1 <= t0:
        return 0
    total = 0.0
    d = t0.date(); end_d = t1.date()
    while d <= end_d:
        j = jornada_del_dia(d)
        if j:
            ji, jf, ci, cf = j
            a, b = max(t0, ji), min(t1, jf)
            if b > a:
                mins = (b - a).total_seconds() / 60.0
                ca, cb = max(a, ci), min(b, cf)
                if cb > ca:
                    mins -= (cb - ca).total_seconds() / 60.0
                total += max(0, mins)
        d += timedelta(days=1)
    return int(round(total))

def minutos_en_velada(t0, t1):
    if not t0 or not t1 or t1 <= t0:
        return 0
    total = 0.0
    d = t0.date() - timedelta(days=1); end_d = t1.date()
    while d <= end_d:
        v0 = datetime.combine(d, dtime(18, 0))
        v1 = datetime.combine(d + timedelta(days=1), dtime(2, 0))
        a, b = max(t0, v0), min(t1, v1)
        if b > a:
            total += (b - a).total_seconds() / 60.0
        d += timedelta(days=1)
    return int(round(total))

def minutos_trabajados(fi, hi, ff, hf):
    t0, t1 = parse_dt(fi, hi), parse_dt(ff, hf)
    if not t0:
        return {"jornada": 0, "velada": 0, "total": 0, "abierto": True}
    if not t1:
        return {"jornada": 0, "velada": 0, "total": 0, "abierto": True}
    j, v = minutos_en_jornada(t0, t1), minutos_en_velada(t0, t1)
    return {"jornada": j, "velada": v, "total": j + v, "abierto": False}

def cabezas_maq(mid):
    return MAQ_BY_ID.get(mid, {}).get("cabezas", 1)

def nombre_maq(mid):
    return MAQ_BY_ID.get(mid, {}).get("nombre", mid or "-")

def badge_estado(est):
    m = {
        "Recibido": ("badge-info", "Recibido"),
        "Asignado": ("badge-warn", "Asignado"),
        "En proceso": ("badge-warn", "En proceso"),
        "Terminado": ("badge-ok", "Terminado"),
        "Cancelado": ("badge-pend", "Cancelado"),
        "Pendiente": ("badge-pend", "Pendiente"),
    }
    cls, lab = m.get(est or "", ("badge-info", est or "-"))
    return '<span class="badge %s">%s</span>' % (cls, lab)


CSS = (
"* {box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#f1f5f9;color:#0f172a}"
".nav{background:#0f172a;color:#fff;padding:.65rem 1rem;display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}"
".nav a{color:#e2e8f0;text-decoration:none;font-size:.9rem}.nav .brand{font-weight:700;color:#fff;margin-right:.5rem}"
".container{max-width:1100px;margin:0 auto;padding:1rem}"
".card{background:#fff;border-radius:12px;padding:1rem 1.15rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(0,0,0,.06)}"
"h1{font-size:1.35rem;margin:0 0 .5rem}h2{font-size:1.05rem;margin:0 0 .6rem}"
".btn{display:inline-block;padding:.45rem .8rem;border-radius:8px;border:none;cursor:pointer;font-size:.88rem;text-decoration:none}"
".btn-primary{background:#4f46e5;color:#fff}.btn-success{background:#059669;color:#fff}"
".btn-warning{background:#d97706;color:#fff}.btn-outline{background:#fff;border:1px solid #cbd5e1;color:#334155}"
".btn-danger{background:#fff;border:1px solid #fca5a5;color:#dc2626}"
".btn-sm{padding:.3rem .55rem;font-size:.8rem}"
".badge{display:inline-block;padding:.15rem .45rem;border-radius:999px;font-size:.72rem;font-weight:600}"
".badge-ok{background:#d1fae5;color:#065f46}.badge-warn{background:#fef3c7;color:#92400e}"
".badge-info{background:#dbeafe;color:#1e40af}.badge-pend{background:#e2e8f0;color:#475569}"
"table{width:100%;border-collapse:collapse;font-size:.88rem}"
"th,td{padding:.45rem .5rem;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}"
"th{font-size:.75rem;text-transform:uppercase;color:#64748b}"
"label{display:block;font-size:.8rem;color:#475569;margin:.35rem 0 .15rem}"
"input,select,textarea{width:100%;padding:.45rem .55rem;border:1px solid #cbd5e1;border-radius:8px;font-size:.9rem}"
".form-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.6rem}"
".alert{padding:.7rem .9rem;border-radius:8px;margin-bottom:.8rem}"
".alert-err{background:#fee2e2;color:#991b1b}.alert-ok{background:#d1fae5;color:#065f46}"
".grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.6rem}"
".stat{background:#f8fafc;border-radius:10px;padding:.75rem;text-align:center}"
".stat .n{font-size:1.35rem;font-weight:700}.stat .l{font-size:.72rem;color:#64748b}"
".bordado-card{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:.7rem;margin:.4rem 0}"
)

def page(title, body, sess=None):
    sess = sess or session
    nav = ""
    if sess.get("user_id"):
        extra = '<a href="/usuarios">Usuarios</a>' if is_admin_or_gerente() else ""
        nav = (
            '<div class="nav"><span class="brand">Bordado</span>'
            '<a href="/">Inicio</a><a href="/trabajos">Trabajos</a>'
            '<a href="/mis-trabajos">Mis trabajos</a>'
            '<a href="/trabajo/nuevo">+ Recibir</a>'
            '<a href="/tablero">Tablero</a><a href="/productividad">Productividad</a>'
            + extra
            + '<span style="margin-left:auto;font-size:.8rem;opacity:.85">'
            + (sess.get("nombre") or sess.get("username") or "")
            + ' | <a href="/logout">Salir</a></span></div>'
        )
    return render_template_string(
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>{{title}} - Bordado</title><style>" + CSS + "</style></head><body>"
        + nav + '<div class="container"><div style="text-align:right;font-size:.65rem;color:#94a3b8">v '
        + APP_VERSION + "</div>" + body + "</div></body></html>",
        title=title,
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        try:
            conn = get_db(); cur = dcur(conn)
            cur.execute("SELECT * FROM usuarios WHERE username=" + ph(), (user,))
            u = as_dict(cur.fetchone()); conn.close()
        except Exception as e:
            err = "Error de base de datos: %s" % e
            u = None
        if u and u.get("password_hash") == h(pw):
            session["user_id"] = u.get("id")
            session["username"] = u.get("username")
            session["nombre"] = u.get("nombre")
            session["rol"] = u.get("rol")
            return redirect(url_for("inicio"))
        if not err:
            err = "Usuario o contrasena incorrectos"
    body = (
        '<div class="card" style="max-width:400px;margin:2rem auto"><h1>Sistema de Bordado</h1>'
        '<p style="color:#64748b;font-size:.9rem">Recepcion, asignacion, maquinas y productividad</p>'
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST"><label>Usuario</label><input name="username" autofocus>'
        '<label>Contrasena</label><input type="password" name="password">'
        '<button class="btn btn-primary" style="margin-top:.8rem;width:100%">Entrar</button></form>'
        '<p style="font-size:.75rem;color:#94a3b8;margin-top:1rem">'
        "admin/admin123 | gerente/gerente123 | operador/operador123</p></div>"
    )
    return page("Login", body)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/health")
def health():
    return {"ok": True, "version": APP_VERSION, "postgres": USE_POSTGRES}


@app.route("/")
@login_required
def inicio():
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT COUNT(*) as c FROM trabajos WHERE estado!='Terminado' AND estado!='Cancelado'")
    abiertos = as_dict(cur.fetchone()).get("c") or 0
    hoy = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) as c, COALESCE(SUM(piezas),0) as p FROM registros WHERE fecha_inicio=" + ph(), (hoy,))
    r = as_dict(cur.fetchone())
    cur.execute("SELECT COUNT(*) as c FROM asignaciones WHERE estado='Pendiente' OR estado='En proceso'")
    asig = as_dict(cur.fetchone()).get("c") or 0
    cur.execute("SELECT * FROM registros WHERE fecha_inicio=" + ph() + " ORDER BY id DESC LIMIT 40", (hoy,))
    regs = as_dicts(cur.fetchall())
    conn.close()
    cards = ""
    for m in MAQUINAS:
        items = [x for x in regs if x.get("maquina_id") == m["id"]][:3]
        if items:
            lines = "".join(
                "<div style='font-size:.82rem;margin:.25rem 0'><strong>%s</strong> - %s pzas - %s-%s</div>"
                % (x.get("operador"), x.get("piezas") or 0, x.get("hora_inicio") or "", x.get("hora_fin") or "...")
                for x in items)
        else:
            lines = "<div style='color:#94a3b8;font-size:.82rem'>Sin actividad hoy</div>"
        cards += (
            '<div class="card" style="margin:0"><h2 style="font-size:.95rem">%s</h2>'
            '<div style="font-size:.75rem;color:#64748b">%s cabezas/ciclo</div>%s</div>'
        ) % (m["nombre"], m["cabezas"], lines)
    body = (
        '<div class="card"><h1>Inicio</h1><div class="grid3">'
        '<div class="stat"><div class="n">%s</div><div class="l">Trabajos activos</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Asignaciones abiertas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Registros hoy</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas hoy</div></div></div>'
        '<div style="margin-top:1rem;display:flex;gap:.5rem;flex-wrap:wrap">'
        '<a class="btn btn-primary" href="/trabajo/nuevo">+ Recibir trabajo</a>'
        '<a class="btn btn-success" href="/mis-trabajos">Mis trabajos</a>'
        '<a class="btn btn-outline" href="/tablero">Tablero</a>'
        '<a class="btn btn-outline" href="/productividad">Productividad</a></div></div>'
        '<h2 style="margin:.5rem 0">Maquinas hoy</h2>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.7rem">%s</div>'
    ) % (abiertos, asig, r.get("c") or 0, r.get("p") or 0, cards)
    return page("Inicio", body, session)

@app.route("/trabajos")
@login_required
def trabajos():
    q = (request.args.get("q") or "").strip()
    conn = get_db(); cur = dcur(conn)
    if q:
        like = "%" + q + "%"
        if USE_POSTGRES:
            cur.execute(
                "SELECT * FROM trabajos WHERE vale ILIKE %s OR proyecto ILIKE %s OR cliente ILIKE %s "
                "ORDER BY id DESC LIMIT 200", (like, like, like))
        else:
            cur.execute(
                "SELECT * FROM trabajos WHERE vale LIKE ? OR proyecto LIKE ? OR cliente LIKE ? "
                "ORDER BY id DESC LIMIT 200", (like, like, like))
    else:
        cur.execute("SELECT * FROM trabajos ORDER BY id DESC LIMIT 200")
    rows = as_dicts(cur.fetchall()); conn.close()
    tr = ""
    for tw in rows:
        titulo = tw.get("vale") or tw.get("proyecto") or ("#" + str(tw["id"]))
        sub = []
        if tw.get("vale") and tw.get("proyecto"):
            sub.append(tw.get("proyecto"))
        if tw.get("cliente"):
            sub.append(tw.get("cliente"))
        tr += (
            '<tr><td><a href="/trabajo/%s"><strong>%s</strong></a><br>'
            '<small style="color:#64748b">%s</small></td>'
            '<td>%s</td><td>%s</td><td>%s</td>'
            '<td><a class="btn btn-outline btn-sm" href="/trabajo/%s">Ver</a></td></tr>'
        ) % (
            tw["id"], titulo, " | ".join(sub),
            badge_estado(tw.get("estado")),
            tw.get("recibido_fecha") or (tw.get("creado_en") or "")[:16],
            tw.get("piezas_recibidas") or 0,
            tw["id"],
        )
    body = (
        '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">'
        '<h1 style="margin:0">Trabajos</h1>'
        '<a class="btn btn-primary" href="/trabajo/nuevo">+ Recibir trabajo</a></div>'
        '<form method="GET" style="margin:.7rem 0;display:flex;gap:.4rem">'
        '<input name="q" value="%s" placeholder="Buscar vale, proyecto, cliente" style="max-width:280px">'
        '<button class="btn btn-outline">Buscar</button></form>'
        '<table><thead><tr><th>Trabajo</th><th>Estado</th><th>Recepcion</th><th>Pzas rec.</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
    ) % (q, tr or "<tr><td colspan=5>Sin trabajos</td></tr>")
    return page("Trabajos", body, session)

@app.route("/trabajo/nuevo", methods=["GET", "POST"])
@login_required
@edit_required
def trabajo_nuevo():
    err = ""
    if request.method == "POST":
        vale = (request.form.get("vale") or "").strip()
        proyecto = (request.form.get("proyecto") or "").strip()
        cliente = (request.form.get("cliente") or "").strip()
        recibido_de = (request.form.get("recibido_de") or "").strip()
        recibido_fecha = (request.form.get("recibido_fecha") or "").strip() or datetime.now().strftime("%Y-%m-%d")
        try:
            piezas_rec = int(request.form.get("piezas_recibidas") or 0)
        except Exception:
            piezas_rec = 0
        obs_rec = (request.form.get("obs_recepcion") or "").strip()
        obs = (request.form.get("observaciones") or "").strip()
        if not vale and not proyecto:
            err = "Indica al menos Vale o Proyecto"
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = get_db(); cur = dcur(conn)
            if USE_POSTGRES:
                cur.execute(
                    "INSERT INTO trabajos (vale,proyecto,cliente,estado,recibido_de,recibido_fecha,recibido_por,"
                    "piezas_recibidas,obs_recepcion,observaciones,creado_por,creado_en) "
                    "VALUES (%s,%s,%s,'Recibido',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (vale, proyecto, cliente, recibido_de, recibido_fecha, session.get("username"),
                     piezas_rec, obs_rec, obs, session.get("username"), now))
                tid = as_dict(cur.fetchone())["id"]
            else:
                cur.execute(
                    "INSERT INTO trabajos (vale,proyecto,cliente,estado,recibido_de,recibido_fecha,recibido_por,"
                    "piezas_recibidas,obs_recepcion,observaciones,creado_por,creado_en) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (vale, proyecto, cliente, "Recibido", recibido_de, recibido_fecha, session.get("username"),
                     piezas_rec, obs_rec, obs, session.get("username"), now))
                tid = cur.lastrowid
            # bordados dinamicos
            nombres = request.form.getlist("b_nombre[]")
            puntadas = request.form.getlist("b_puntadas[]")
            mins = request.form.getlist("b_mins[]")
            modelos = request.form.getlist("b_modelo[]")
            colores = request.form.getlist("b_color[]")
            n_ok = 0
            for i, nom in enumerate(nombres):
                nom = (nom or "").strip()
                if not nom:
                    continue
                try:
                    pu = int(puntadas[i] or 0)
                except Exception:
                    pu = 0
                try:
                    mi = float(str((mins[i] if i < len(mins) else 0) or 0).replace(",", "."))
                except Exception:
                    mi = 0
                mod = (modelos[i] if i < len(modelos) else "") or ""
                col = (colores[i] if i < len(colores) else "") or ""
                if USE_POSTGRES:
                    cur.execute(
                        "INSERT INTO bordados (trabajo_id,nombre,puntadas,minutos_proyectados,modelo,color) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (tid, nom, pu, mi, mod.strip(), col.strip()))
                else:
                    cur.execute(
                        "INSERT INTO bordados (trabajo_id,nombre,puntadas,minutos_proyectados,modelo,color) "
                        "VALUES (?,?,?,?,?,?)", (tid, nom, pu, mi, mod.strip(), col.strip()))
                n_ok += 1
            log_evento(cur, tid, "Recepcion",
                       "Recibido de %s | %s pzas | %s bordados" % (recibido_de or "-", piezas_rec, n_ok))
            conn.commit(); conn.close()
            return redirect(url_for("trabajo_ver", trabajo_id=tid))
    hoy = datetime.now().strftime("%Y-%m-%d")
    body = (
        '<div class="card"><h1>Recibir trabajo</h1>'
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST" id="f">'
        '<h2>1. Identificacion</h2><div class="form-row">'
        '<div><label>Vale de bordado</label><input name="vale" placeholder="Opcional si hay proyecto"></div>'
        '<div><label>Proyecto</label><input name="proyecto" placeholder="Opcional si hay vale"></div>'
        '<div><label>Cliente</label><input name="cliente"></div></div>'
        '<h2 style="margin-top:1rem">2. Recepcion</h2><div class="form-row">'
        '<div><label>Quien entrega / de donde viene</label><input name="recibido_de" placeholder="Area o persona"></div>'
        '<div><label>Fecha de recepcion</label><input type="date" name="recibido_fecha" value="' + hoy + '"></div>'
        '<div><label>Piezas recibidas</label><input type="number" name="piezas_recibidas" min="0" value="0"></div></div>'
        '<label>Obs. recepcion</label><textarea name="obs_recepcion" rows="2"></textarea>'
        '<h2 style="margin-top:1rem">3. Bordados del proyecto</h2>'
        '<p style="font-size:.82rem;color:#64748b">Un proyecto puede tener uno o varios bordados. '
        "Siempre hay uno; usa el boton para agregar mas.</p>"
        '<div id="lista_bordados"></div>'
        '<button type="button" class="btn btn-outline btn-sm" onclick="addBordado()">+ Agregar otro bordado</button>'
        '<label style="margin-top:.8rem">Observaciones generales</label><textarea name="observaciones" rows="2"></textarea>'
        '<div style="margin-top:1rem"><button class="btn btn-primary">Guardar recepcion</button> '
        '<a class="btn btn-outline" href="/trabajos">Cancelar</a></div></form></div>'
        """<script>
        function addBordado(){
          var el=document.getElementById('lista_bordados');
          var n=el.children.length+1;
          var div=document.createElement('div');
          div.className='bordado-card';
          div.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center">'
            +'<strong>Bordado '+n+'</strong>'
            +(n>1?'<button type="button" class="btn btn-danger btn-sm" onclick="this.parentNode.parentNode.remove()">Quitar</button>':'')
            +'</div><div class="form-row">'
            +'<div><label>Nombre del bordado</label><input name="b_nombre[]" required placeholder="Ej. Logo pecho"></div>'
            +'<div><label>Puntadas</label><input type="number" name="b_puntadas[]" min="0" value="0"></div>'
            +'<div><label>Min proyectados / vuelta</label><input type="number" step="0.1" name="b_mins[]" min="0" value="0"></div>'
            +'<div><label>Modelo</label><input name="b_modelo[]"></div>'
            +'<div><label>Color</label><input name="b_color[]"></div></div>';
          el.appendChild(div);
        }
        addBordado();
        </script>"""
    )
    return page("Recibir trabajo", body, session)


@app.route("/trabajo/<int:trabajo_id>")
@login_required
def trabajo_ver(trabajo_id):
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT * FROM trabajos WHERE id=" + ph(), (trabajo_id,))
    tw = as_dict(cur.fetchone())
    if not tw:
        conn.close()
        return redirect(url_for("trabajos"))
    cur.execute("SELECT * FROM bordados WHERE trabajo_id=" + ph() + " ORDER BY id", (trabajo_id,))
    bordados = as_dicts(cur.fetchall())
    cur.execute("SELECT * FROM asignaciones WHERE trabajo_id=" + ph() + " ORDER BY id", (trabajo_id,))
    asigs = as_dicts(cur.fetchall())
    cur.execute("SELECT * FROM registros WHERE trabajo_id=" + ph() + " ORDER BY id DESC LIMIT 100", (trabajo_id,))
    regs = as_dicts(cur.fetchall())
    cur.execute("SELECT * FROM historial WHERE trabajo_id=" + ph() + " ORDER BY id DESC LIMIT 50", (trabajo_id,))
    hist = as_dicts(cur.fetchall())
    conn.close()
    titulo = tw.get("vale") or tw.get("proyecto") or ("#" + str(tw["id"]))

    brows = "".join(
        "<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (b.get("nombre"), b.get("puntadas") or 0, b.get("minutos_proyectados") or 0,
           b.get("modelo") or "-", b.get("color") or "-")
        for b in bordados)

    opts_b = '<option value="">- Cualquiera / general -</option>'
    for b in bordados:
        opts_b += '<option value="%s">%s</option>' % (b["id"], b.get("nombre"))
    opts_m = "".join(
        '<option value="%s">%s</option>' % (m["id"], m["nombre"]) for m in MAQUINAS)

    arows = ""
    for a in asigs:
        arows += (
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            '<td><a class="btn btn-success btn-sm" href="/registro/nuevo?asignacion_id=%s">Reportar</a></td></tr>'
        ) % (
            a.get("operador"), nombre_maq(a.get("maquina_id")) if a.get("maquina_id") else "-",
            a.get("piezas_objetivo") or 0, badge_estado(a.get("estado")),
            (a.get("fecha_asignacion") or "")[:16], a["id"],
        )

    rrows = ""
    for r in regs:
        mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"))
        pzas = int(r.get("piezas") or 0)
        pu = int(r.get("puntadas_unit") or 0)
        rrows += (
            "<tr><td>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s/%s</td><td>%s</td>"
            '<td><a class="btn btn-outline btn-sm" href="/registro/%s">Ver</a></td></tr>'
        ) % (
            r.get("fecha_inicio") or "", r.get("hora_inicio") or "",
            r.get("operador"), nombre_maq(r.get("maquina_id")),
            pzas, "{:,}".format(pu * pzas),
            mt["jornada"], mt["velada"],
            (r.get("incidencia") or "-")[:40],
            r["id"],
        )

    hrows = "".join(
        "<tr><td>%s</td><td><strong>%s</strong></td><td>%s</td><td>%s</td></tr>"
        % (h.get("fecha") or "", h.get("evento") or "", h.get("detalle") or "", h.get("usuario") or "")
        for h in hist)

    form_asig = ""
    if is_admin_or_gerente():
        form_asig = (
            '<div class="card"><h2>Asignar operador(es)</h2>'
            '<p style="font-size:.82rem;color:#64748b">Un proyecto grande puede repartirse en 2 o 3 operadores, cada uno en su maquina.</p>'
            '<form method="POST" action="/trabajo/%s/asignar"><div class="form-row">'
            '<div><label>Operador</label><input name="operador" required placeholder="Nombre"></div>'
            '<div><label>Maquina</label><select name="maquina_id"><option value="">- Opcional -</option>%s</select></div>'
            '<div><label>Bordado</label><select name="bordado_id">%s</select></div>'
            '<div><label>Piezas objetivo</label><input type="number" name="piezas_objetivo" min="0" value="0"></div></div>'
            '<label>Obs.</label><textarea name="observaciones" rows="1"></textarea>'
            '<button class="btn btn-primary btn-sm" style="margin-top:.4rem">Asignar</button></form></div>'
        ) % (trabajo_id, opts_m, opts_b)

    body = (
        '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">'
        '<div><h1 style="margin:0">%s</h1>'
        '<div style="font-size:.85rem;color:#64748b">Vale: <strong>%s</strong> | Proyecto: <strong>%s</strong> | Cliente: %s</div>'
        '%s</div>'
        '<div><a class="btn btn-success" href="/registro/nuevo?trabajo_id=%s">+ Reportar</a> '
        '<a class="btn btn-outline" href="/trabajos">Volver</a></div></div>'
        '<div class="grid3" style="margin-top:.8rem">'
        '<div class="stat"><div class="n" style="font-size:1rem">%s</div><div class="l">Entregado por</div></div>'
        '<div class="stat"><div class="n" style="font-size:1rem">%s</div><div class="l">Fecha recepcion</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas recibidas</div></div>'
        '<div class="stat"><div class="n" style="font-size:.9rem">%s</div><div class="l">Recibido por</div></div></div>'
        '<p style="font-size:.85rem;margin-top:.5rem">%s</p></div>'
        '<div class="card"><h2>Bordados</h2>'
        '<table><thead><tr><th>Bordado</th><th>Puntadas</th><th>Min/vuelta</th><th>Modelo</th><th>Color</th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
        '%s'
        '<div class="card"><h2>Asignaciones (operadores)</h2>'
        '<table><thead><tr><th>Operador</th><th>Maquina</th><th>Pzas obj.</th><th>Estado</th><th>Fecha</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
        '<div class="card"><h2>Registros de productividad</h2>'
        '<table><thead><tr><th>Inicio</th><th>Operador</th><th>Maquina</th><th>Piezas</th><th>Puntadas</th>'
        '<th>Min j/v</th><th>Incidencia</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
        '<div class="card"><h2>Trazabilidad</h2>'
        '<table style="font-size:.82rem"><thead><tr><th>Fecha</th><th>Evento</th><th>Detalle</th><th>Usuario</th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
    ) % (
        titulo, tw.get("vale") or "-", tw.get("proyecto") or "-", tw.get("cliente") or "-",
        badge_estado(tw.get("estado")), trabajo_id,
        tw.get("recibido_de") or "-", tw.get("recibido_fecha") or "-",
        tw.get("piezas_recibidas") or 0, tw.get("recibido_por") or "-",
        tw.get("observaciones") or tw.get("obs_recepcion") or "",
        brows or "<tr><td colspan=5>Sin bordados</td></tr>",
        form_asig,
        arows or "<tr><td colspan=6>Sin asignaciones. Gerente puede asignar arriba.</td></tr>",
        rrows or "<tr><td colspan=8>Sin registros</td></tr>",
        hrows or "<tr><td colspan=4>Sin historial</td></tr>",
    )
    return page(titulo, body, session)

@app.route("/trabajo/<int:trabajo_id>/asignar", methods=["POST"])
@login_required
@edit_required
def trabajo_asignar(trabajo_id):
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin asigna.</div>', session)
    operador = (request.form.get("operador") or "").strip()
    maquina_id = (request.form.get("maquina_id") or "").strip()
    try:
        bordado_id = int(request.form.get("bordado_id") or 0) or None
    except Exception:
        bordado_id = None
    try:
        pzas = int(request.form.get("piezas_objetivo") or 0)
    except Exception:
        pzas = 0
    obs = (request.form.get("observaciones") or "").strip()
    if not operador:
        return redirect(url_for("trabajo_ver", trabajo_id=trabajo_id))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute(
            "INSERT INTO asignaciones (trabajo_id,bordado_id,operador,maquina_id,piezas_objetivo,estado,"
            "fecha_asignacion,asignado_por,observaciones) VALUES (%s,%s,%s,%s,%s,'Pendiente',%s,%s,%s)",
            (trabajo_id, bordado_id, operador, maquina_id, pzas, now, session.get("username"), obs))
        cur.execute("UPDATE trabajos SET estado=%s WHERE id=%s AND estado IN ('Recibido','Asignado')",
                    ("Asignado", trabajo_id))
    else:
        cur.execute(
            "INSERT INTO asignaciones (trabajo_id,bordado_id,operador,maquina_id,piezas_objetivo,estado,"
            "fecha_asignacion,asignado_por,observaciones) VALUES (?,?,?,?,?,'Pendiente',?,?,?)",
            (trabajo_id, bordado_id, operador, maquina_id, pzas, now, session.get("username"), obs))
        cur.execute("UPDATE trabajos SET estado=? WHERE id=? AND estado IN ('Recibido','Asignado')",
                    ("Asignado", trabajo_id))
    log_evento(cur, trabajo_id, "Asignacion",
               "%s | maquina %s | obj %s pzas" % (operador, nombre_maq(maquina_id) if maquina_id else "-", pzas))
    conn.commit(); conn.close()
    return redirect(url_for("trabajo_ver", trabajo_id=trabajo_id))

@app.route("/mis-trabajos")
@login_required
def mis_trabajos():
    nombre = (session.get("nombre") or session.get("username") or "").strip()
    conn = get_db(); cur = dcur(conn)
    if is_admin_or_gerente():
        cur.execute(
            "SELECT a.*, t.vale, t.proyecto, t.estado as estado_trabajo FROM asignaciones a "
            "JOIN trabajos t ON t.id=a.trabajo_id WHERE a.estado!='Terminado' ORDER BY a.id DESC LIMIT 100")
    else:
        if USE_POSTGRES:
            cur.execute(
                "SELECT a.*, t.vale, t.proyecto, t.estado as estado_trabajo FROM asignaciones a "
                "JOIN trabajos t ON t.id=a.trabajo_id WHERE a.estado!='Terminado' AND a.operador ILIKE %s "
                "ORDER BY a.id DESC LIMIT 100", ("%" + nombre + "%",))
        else:
            cur.execute(
                "SELECT a.*, t.vale, t.proyecto, t.estado as estado_trabajo FROM asignaciones a "
                "JOIN trabajos t ON t.id=a.trabajo_id WHERE a.estado!='Terminado' AND a.operador LIKE ? "
                "ORDER BY a.id DESC LIMIT 100", ("%" + nombre + "%",))
    rows = as_dicts(cur.fetchall()); conn.close()
    tr = ""
    for a in rows:
        titulo = a.get("vale") or a.get("proyecto") or ("#" + str(a.get("trabajo_id")))
        tr += (
            '<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
            '<td><a class="btn btn-success btn-sm" href="/registro/nuevo?asignacion_id=%s">Reportar trabajo</a> '
            '<a class="btn btn-outline btn-sm" href="/trabajo/%s">Ver</a></td></tr>'
        ) % (
            titulo, a.get("operador"), nombre_maq(a.get("maquina_id")) if a.get("maquina_id") else "-",
            a.get("piezas_objetivo") or 0, badge_estado(a.get("estado")),
            a["id"], a.get("trabajo_id"),
        )
    body = (
        '<div class="card"><h1>Mis trabajos</h1>'
        '<p style="color:#64748b;font-size:.9rem">Asignaciones pendientes o en proceso. Reporta inicio, fin, piezas e incidencias.</p>'
        '<table><thead><tr><th>Trabajo</th><th>Operador</th><th>Maquina</th><th>Obj.</th><th>Estado</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
    ) % (tr or "<tr><td colspan=6>No tienes asignaciones abiertas</td></tr>")
    return page("Mis trabajos", body, session)


@app.route("/registro/nuevo", methods=["GET", "POST"])
@login_required
@edit_required
def registro_nuevo():
    err = ""
    asignacion_id = request.args.get("asignacion_id") or request.form.get("asignacion_id") or ""
    trabajo_id = request.args.get("trabajo_id") or request.form.get("trabajo_id") or ""
    conn = get_db(); cur = dcur(conn)
    asig = {}
    if asignacion_id:
        try:
            cur.execute("SELECT a.*, t.vale, t.proyecto FROM asignaciones a JOIN trabajos t ON t.id=a.trabajo_id WHERE a.id=" + ph(),
                        (int(asignacion_id),))
            asig = as_dict(cur.fetchone())
            if asig:
                trabajo_id = str(asig.get("trabajo_id") or "")
        except Exception:
            pass
    cur.execute("SELECT * FROM trabajos WHERE estado!='Cancelado' ORDER BY id DESC LIMIT 100")
    trabajos_list = as_dicts(cur.fetchall())
    bordados = []
    if trabajo_id:
        try:
            cur.execute("SELECT * FROM bordados WHERE trabajo_id=" + ph() + " ORDER BY id", (int(trabajo_id),))
            bordados = as_dicts(cur.fetchall())
        except Exception:
            pass

    if request.method == "POST":
        try:
            tid = int(request.form.get("trabajo_id") or 0)
        except Exception:
            tid = 0
        try:
            aid = int(request.form.get("asignacion_id") or 0) or None
        except Exception:
            aid = None
        try:
            bid = int(request.form.get("bordado_id") or 0) or None
        except Exception:
            bid = None
        operador = (request.form.get("operador") or "").strip()
        maquina_id = (request.form.get("maquina_id") or "").strip()
        try:
            piezas = int(request.form.get("piezas") or 0)
        except Exception:
            piezas = 0
        puntadas_unit = 0
        mins_proy = 0.0
        if bid:
            cur.execute("SELECT * FROM bordados WHERE id=" + ph(), (bid,))
            b = as_dict(cur.fetchone())
            if b:
                puntadas_unit = int(b.get("puntadas") or 0)
                mins_proy = float(b.get("minutos_proyectados") or 0)
        try:
            if request.form.get("puntadas_unit"):
                puntadas_unit = int(request.form.get("puntadas_unit") or puntadas_unit)
        except Exception:
            pass
        try:
            if request.form.get("minutos_proyectados"):
                mins_proy = float(str(request.form.get("minutos_proyectados") or mins_proy).replace(",", "."))
        except Exception:
            pass
        fi = (request.form.get("fecha_inicio") or "").strip() or datetime.now().strftime("%Y-%m-%d")
        hi = (request.form.get("hora_inicio") or "").strip()
        ff = (request.form.get("fecha_fin") or "").strip()
        hf = (request.form.get("hora_fin") or "").strip()
        incidencia = (request.form.get("incidencia") or "").strip()
        obs = (request.form.get("observaciones") or "").strip()
        if not tid:
            err = "Selecciona un trabajo"
        elif not operador:
            err = "Indica el operador"
        elif not maquina_id:
            err = "Selecciona la maquina"
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            if USE_POSTGRES:
                cur.execute(
                    "INSERT INTO registros (trabajo_id,asignacion_id,bordado_id,operador,maquina_id,piezas,"
                    "puntadas_unit,minutos_proyectados,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
                    "incidencia,observaciones,registrado_por,creado_en) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (tid, aid, bid, operador, maquina_id, piezas, puntadas_unit, mins_proy,
                     fi, hi, ff or None, hf or None, incidencia, obs, session.get("username"), now))
                rid = as_dict(cur.fetchone())["id"]
                cur.execute("UPDATE trabajos SET estado='En proceso' WHERE id=%s AND estado IN ('Recibido','Asignado')", (tid,))
                if aid:
                    cur.execute("UPDATE asignaciones SET estado='En proceso' WHERE id=%s AND estado='Pendiente'", (aid,))
            else:
                cur.execute(
                    "INSERT INTO registros (trabajo_id,asignacion_id,bordado_id,operador,maquina_id,piezas,"
                    "puntadas_unit,minutos_proyectados,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
                    "incidencia,observaciones,registrado_por,creado_en) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, aid, bid, operador, maquina_id, piezas, puntadas_unit, mins_proy,
                     fi, hi, ff or None, hf or None, incidencia, obs, session.get("username"), now))
                rid = cur.lastrowid
                cur.execute("UPDATE trabajos SET estado='En proceso' WHERE id=? AND estado IN ('Recibido','Asignado')", (tid,))
                if aid:
                    cur.execute("UPDATE asignaciones SET estado='En proceso' WHERE id=? AND estado='Pendiente'", (aid,))
            log_evento(cur, tid, "Registro",
                       "%s | %s | %s pzas | incid: %s" % (operador, nombre_maq(maquina_id), piezas, incidencia or "ninguna"))
            conn.commit(); conn.close()
            return redirect(url_for("registro_ver", reg_id=rid))

    conn.close()
    opts_t = '<option value="">- Seleccionar -</option>'
    for tw in trabajos_list:
        lab = tw.get("vale") or tw.get("proyecto") or ("#" + str(tw["id"]))
        if tw.get("vale") and tw.get("proyecto"):
            lab = "%s | %s" % (tw.get("vale"), tw.get("proyecto"))
        sel = " selected" if str(tw["id"]) == str(trabajo_id) else ""
        opts_t += '<option value="%s"%s>%s</option>' % (tw["id"], sel, lab)
    opts_b = '<option value="">- General -</option>'
    for b in bordados:
        sel = ""
        if asig and str(asig.get("bordado_id") or "") == str(b["id"]):
            sel = " selected"
        opts_b += '<option value="%s"%s>%s (%s puntadas)</option>' % (
            b["id"], sel, b.get("nombre"), b.get("puntadas") or 0)
    opts_m = ""
    for m in MAQUINAS:
        sel = " selected" if asig and asig.get("maquina_id") == m["id"] else ""
        opts_m += '<option value="%s"%s>%s (%s cab.)</option>' % (m["id"], sel, m["nombre"], m["cabezas"])
    hoy = datetime.now().strftime("%Y-%m-%d")
    ahora = datetime.now().strftime("%H:%M")
    op_val = asig.get("operador") or (session.get("nombre") if session.get("rol") == "operador" else "")
    body = (
        '<div class="card"><h1>Reportar productividad</h1>'
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST">'
        '<input type="hidden" name="asignacion_id" value="%s">'
        '<div class="form-row">'
        '<div><label>Trabajo</label><select name="trabajo_id" required onchange="location=\'/registro/nuevo?trabajo_id=\'+this.value%s">%s</select></div>'
        '<div><label>Bordado</label><select name="bordado_id">%s</select></div></div>'
        '<div class="form-row">'
        '<div><label>Operador</label><input name="operador" required value="%s"></div>'
        '<div><label>Maquina</label><select name="maquina_id" required>%s</select></div>'
        '<div><label>Piezas realizadas</label><input type="number" name="piezas" min="0" value="0" required></div></div>'
        '<div class="form-row">'
        '<div><label>Puntadas (si no hay bordado)</label><input type="number" name="puntadas_unit" min="0"></div>'
        '<div><label>Min proyectados / vuelta</label><input type="number" step="0.1" name="minutos_proyectados" min="0"></div></div>'
        '<div class="form-row">'
        '<div><label>Fecha inicio</label><input type="date" name="fecha_inicio" value="%s" required></div>'
        '<div><label>Hora inicio</label><input type="time" name="hora_inicio" value="%s" required></div>'
        '<div><label>Fecha fin</label><input type="date" name="fecha_fin"></div>'
        '<div><label>Hora fin</label><input type="time" name="hora_fin"></div></div>'
        '<label>Incidencia (opcional)</label><textarea name="incidencia" rows="2" placeholder="Paros, hilo, maquina, calidad..."></textarea>'
        '<label>Observaciones</label><textarea name="observaciones" rows="2"></textarea>'
        '<p style="font-size:.78rem;color:#64748b">Puedes dejar fin vacio y completarlo despues. Velada 18:00-02:00. Sabado 8:00-17:30.</p>'
        '<button class="btn btn-primary">Guardar registro</button> '
        '<a class="btn btn-outline" href="/mis-trabajos">Cancelar</a></form></div>'
    ) % (
        asignacion_id or "",
        ("+'&asignacion_id=" + str(asignacion_id) + "'") if asignacion_id else "",
        opts_t, opts_b, op_val or "", opts_m, hoy, ahora,
    )
    return page("Nuevo registro", body, session)

@app.route("/registro/<int:reg_id>", methods=["GET", "POST"])
@login_required
def registro_ver(reg_id):
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT * FROM registros WHERE id=" + ph(), (reg_id,))
    r = as_dict(cur.fetchone())
    if not r:
        conn.close()
        return redirect(url_for("inicio"))
    if request.method == "POST" and can_edit():
        ff = (request.form.get("fecha_fin") or "").strip()
        hf = (request.form.get("hora_fin") or "").strip()
        try:
            piezas = int(request.form.get("piezas") or r.get("piezas") or 0)
        except Exception:
            piezas = int(r.get("piezas") or 0)
        incidencia = (request.form.get("incidencia") or "").strip()
        obs = (request.form.get("observaciones") or "").strip()
        if USE_POSTGRES:
            cur.execute(
                "UPDATE registros SET fecha_fin=%s,hora_fin=%s,piezas=%s,incidencia=%s,observaciones=%s WHERE id=%s",
                (ff or None, hf or None, piezas, incidencia, obs, reg_id))
        else:
            cur.execute(
                "UPDATE registros SET fecha_fin=?,hora_fin=?,piezas=?,incidencia=?,observaciones=? WHERE id=?",
                (ff or None, hf or None, piezas, incidencia, obs, reg_id))
        if ff and hf and r.get("asignacion_id"):
            if USE_POSTGRES:
                cur.execute("UPDATE asignaciones SET estado='Terminado' WHERE id=%s", (r.get("asignacion_id"),))
            else:
                cur.execute("UPDATE asignaciones SET estado='Terminado' WHERE id=?", (r.get("asignacion_id"),))
        log_evento(cur, r.get("trabajo_id"), "Actualiza registro", "Reg #%s | %s pzas" % (reg_id, piezas))
        conn.commit()
        cur.execute("SELECT * FROM registros WHERE id=" + ph(), (reg_id,))
        r = as_dict(cur.fetchone())
    cur.execute("SELECT * FROM trabajos WHERE id=" + ph(), (r.get("trabajo_id"),))
    tw = as_dict(cur.fetchone())
    conn.close()
    mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"))
    cab = cabezas_maq(r.get("maquina_id"))
    pzas = int(r.get("piezas") or 0)
    pu = int(r.get("puntadas_unit") or 0)
    mins_proy = float(r.get("minutos_proyectados") or 0)
    vueltas = (pzas / cab) if cab else pzas
    proy_total = vueltas * mins_proy
    eff = (proy_total / mt["total"] * 100) if mt["total"] > 0 and proy_total > 0 else None
    titulo = (tw.get("vale") or tw.get("proyecto") or "") if tw else ""
    form_edit = ""
    if can_edit():
        form_edit = (
            '<form method="POST" class="card"><h2>Actualizar / cerrar</h2><div class="form-row">'
            '<div><label>Piezas</label><input type="number" name="piezas" value="%s" min="0"></div>'
            '<div><label>Fecha fin</label><input type="date" name="fecha_fin" value="%s"></div>'
            '<div><label>Hora fin</label><input type="time" name="hora_fin" value="%s"></div></div>'
            '<label>Incidencia</label><textarea name="incidencia" rows="2">%s</textarea>'
            '<label>Observaciones</label><textarea name="observaciones" rows="2">%s</textarea>'
            '<button class="btn btn-primary" style="margin-top:.5rem">Guardar</button></form>'
        ) % (pzas, r.get("fecha_fin") or "", (r.get("hora_fin") or "")[:5],
             r.get("incidencia") or "", r.get("observaciones") or "")
    body = (
        '<div class="card"><h1>Registro #%s</h1>'
        '<p>Trabajo: <a href="/trabajo/%s"><strong>%s</strong></a></p>'
        '<div class="grid3">'
        '<div class="stat"><div class="n">%s</div><div class="l">Operador</div></div>'
        '<div class="stat"><div class="n" style="font-size:1rem">%s</div><div class="l">%s cabezas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min jornada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min velada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min totales</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min proyectados</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Eficiencia</div></div></div>'
        '<p style="font-size:.85rem;margin-top:.7rem;color:#64748b">Inicio: <strong>%s %s</strong> | Fin: <strong>%s %s</strong>%s</p>'
        '<p style="font-size:.85rem">Incidencia: %s</p></div>%s'
        '<a class="btn btn-outline" href="/trabajo/%s">Volver al trabajo</a>'
    ) % (
        reg_id, r.get("trabajo_id"), titulo or r.get("trabajo_id"),
        r.get("operador"), nombre_maq(r.get("maquina_id")), cab, pzas, "{:,}".format(pu * pzas),
        mt["jornada"], mt["velada"], mt["total"],
        ("%.0f" % proy_total) if proy_total else "-",
        ("%.0f%%" % eff) if eff is not None else "-",
        r.get("fecha_inicio") or "", r.get("hora_inicio") or "",
        r.get("fecha_fin") or "-", r.get("hora_fin") or "",
        ' <span class="badge badge-warn">En proceso</span>' if mt["abierto"] else "",
        r.get("incidencia") or "Ninguna",
        form_edit, r.get("trabajo_id"),
    )
    return page("Registro %s" % reg_id, body, session)


@app.route("/tablero")
@login_required
def tablero():
    hoy = datetime.now().strftime("%Y-%m-%d")
    conn = get_db(); cur = dcur(conn)
    cur.execute(
        "SELECT r.*, t.vale, t.proyecto FROM registros r LEFT JOIN trabajos t ON t.id=r.trabajo_id "
        "WHERE r.fecha_inicio=" + ph() + " OR r.fecha_fin=" + ph() + " ORDER BY r.id DESC LIMIT 200",
        (hoy, hoy))
    regs = as_dicts(cur.fetchall())
    cur.execute(
        "SELECT a.*, t.vale, t.proyecto FROM asignaciones a JOIN trabajos t ON t.id=a.trabajo_id "
        "WHERE a.estado IN ('Pendiente','En proceso') ORDER BY a.id DESC LIMIT 50")
    asigs = as_dicts(cur.fetchall())
    conn.close()
    cards = ""
    for m in MAQUINAS:
        items = [x for x in regs if x.get("maquina_id") == m["id"]]
        abiertos = [x for x in items if not (x.get("fecha_fin") and x.get("hora_fin"))]
        cerrados = [x for x in items if x.get("fecha_fin") and x.get("hora_fin")]
        lista = abiertos + cerrados[:4]
        rows = ""
        for x in lista:
            mt = minutos_trabajados(x.get("fecha_inicio"), x.get("hora_inicio"), x.get("fecha_fin"), x.get("hora_fin"))
            tit = x.get("vale") or x.get("proyecto") or ("#" + str(x.get("trabajo_id")))
            estado = '<span class="badge badge-warn">En proceso</span>' if mt["abierto"] else '<span class="badge badge-ok">Cerrado</span>'
            rows += (
                '<tr><td><a href="/registro/%s">%s</a></td><td>%s</td><td>%s</td>'
                "<td>%s-%s</td><td>%s</td></tr>"
            ) % (
                x["id"], tit, x.get("operador"), x.get("piezas") or 0,
                (x.get("hora_inicio") or "")[:5],
                (x.get("hora_fin") or "...")[:5] if x.get("hora_fin") else "...",
                estado,
            )
        cards += (
            '<div class="card"><h2>%s <small style="color:#64748b">(%s cab.)</small></h2>'
            '<table style="font-size:.82rem"><thead><tr><th>Trabajo</th><th>Operador</th><th>Pzas</th><th>Horario</th><th></th></tr></thead>'
            "<tbody>%s</tbody></table></div>"
        ) % (m["nombre"], m["cabezas"], rows or "<tr><td colspan=5>Sin actividad hoy</td></tr>")
    arows = ""
    for a in asigs:
        tit = a.get("vale") or a.get("proyecto") or ("#" + str(a.get("trabajo_id")))
        arows += (
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            '<td><a class="btn btn-success btn-sm" href="/registro/nuevo?asignacion_id=%s">Reportar</a></td></tr>'
        ) % (
            tit, a.get("operador"), nombre_maq(a.get("maquina_id")) if a.get("maquina_id") else "-",
            a.get("piezas_objetivo") or 0, badge_estado(a.get("estado")), a["id"],
        )
    body = (
        '<div class="card"><h1>Tablero</h1>'
        '<p style="color:#64748b">Actividad de hoy (%s) y asignaciones abiertas</p></div>'
        '<div class="card"><h2>Asignaciones abiertas</h2>'
        '<table style="font-size:.82rem"><thead><tr><th>Trabajo</th><th>Operador</th><th>Maquina</th><th>Obj.</th><th>Estado</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>%s'
    ) % (hoy, arows or "<tr><td colspan=6>Sin asignaciones abiertas</td></tr>", cards)
    return page("Tablero", body, session)

@app.route("/productividad")
@login_required
def productividad():
    periodo = (request.args.get("periodo") or "semana").strip()
    fecha_ref = (request.args.get("fecha") or datetime.now().strftime("%Y-%m-%d")).strip()
    try:
        ref = datetime.strptime(fecha_ref[:10], "%Y-%m-%d").date()
    except Exception:
        ref = datetime.now().date()
        fecha_ref = ref.strftime("%Y-%m-%d")
    if periodo == "dia":
        fi = ff = ref
        titulo = "Dia " + ref.strftime("%d/%m/%Y")
    elif periodo == "mes":
        fi = ref.replace(day=1)
        if ref.month == 12:
            ff = ref.replace(year=ref.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            ff = ref.replace(month=ref.month + 1, day=1) - timedelta(days=1)
        titulo = "Mes " + ref.strftime("%m/%Y")
    else:
        periodo = "semana"
        fi = ref - timedelta(days=ref.weekday())
        ff = fi + timedelta(days=6)
        titulo = "Semana %s - %s" % (fi.strftime("%d/%m"), ff.strftime("%d/%m/%Y"))
    fi_s, ff_s = fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d")
    conn = get_db(); cur = dcur(conn)
    cur.execute(
        "SELECT * FROM registros WHERE fecha_inicio>=" + ph() + " AND fecha_inicio<=" + ph() + " ORDER BY fecha_inicio, id",
        (fi_s, ff_s))
    regs = as_dicts(cur.fetchall()); conn.close()
    by_op, by_maq = {}, {}
    tot_pzas = tot_punt = tot_j = tot_v = tot_inc = 0
    for r in regs:
        op = (r.get("operador") or "-").strip()
        mid = r.get("maquina_id") or "?"
        pzas = int(r.get("piezas") or 0)
        pu = int(r.get("puntadas_unit") or 0)
        punt = pzas * pu
        mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"))
        cab = cabezas_maq(mid)
        mins_proy = float(r.get("minutos_proyectados") or 0)
        vueltas = (pzas / cab) if cab else pzas
        proy = vueltas * mins_proy
        has_inc = 1 if (r.get("incidencia") or "").strip() else 0
        for bucket, key in ((by_op, op), (by_maq, mid)):
            st = bucket.setdefault(key, {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "proy": 0.0, "regs": 0, "inc": 0})
            st["pzas"] += pzas; st["punt"] += punt
            st["jornada"] += mt["jornada"]; st["velada"] += mt["velada"]; st["total"] += mt["total"]
            st["proy"] += proy; st["regs"] += 1; st["inc"] += has_inc
        tot_pzas += pzas; tot_punt += punt; tot_j += mt["jornada"]; tot_v += mt["velada"]; tot_inc += has_inc

    def rows_from(bucket, label_fn):
        out = ""
        for k, st in sorted(bucket.items(), key=lambda x: -x[1]["pzas"]):
            eff = (st["proy"] / st["total"] * 100) if st["total"] and st["proy"] else None
            out += (
                "<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%.0f</td><td>%s</td><td>%s</td></tr>"
            ) % (
                label_fn(k), st["regs"], st["pzas"], "{:,}".format(st["punt"]),
                st["jornada"], st["velada"], st["total"], st["proy"],
                ("%.0f%%" % eff) if eff is not None else "-", st["inc"],
            )
        return out or "<tr><td colspan=10>Sin datos</td></tr>"

    tabs = ""
    for key, lab in [("dia", "Dia"), ("semana", "Semana"), ("mes", "Mes")]:
        cls = "btn-primary" if periodo == key else "btn-outline"
        tabs += '<a class="btn %s btn-sm" href="/productividad?periodo=%s&fecha=%s">%s</a> ' % (cls, key, fecha_ref, lab)
    body = (
        '<div class="card"><h1>Productividad e indicadores</h1><p style="color:#64748b">%s</p>'
        '<div style="display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin-bottom:.7rem">%s'
        '<form method="GET" style="display:inline-flex;gap:.35rem;align-items:center">'
        '<input type="hidden" name="periodo" value="%s">'
        '<input type="date" name="fecha" value="%s">'
        '<button class="btn btn-outline btn-sm">Ver</button></form></div>'
        '<div class="grid3">'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min jornada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min velada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min totales</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Incidencias</div></div></div></div>'
        '<div class="card"><h2>Por operador</h2>'
        '<table style="font-size:.82rem"><thead><tr><th>Operador</th><th>Regs</th><th>Piezas</th><th>Puntadas</th>'
        '<th>Min jor</th><th>Min vel</th><th>Min tot</th><th>Min proy</th><th>Efic.</th><th>Incid.</th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
        '<div class="card"><h2>Por maquina (aprovechamiento)</h2>'
        '<table style="font-size:.82rem"><thead><tr><th>Maquina</th><th>Regs</th><th>Piezas</th><th>Puntadas</th>'
        '<th>Min jor</th><th>Min vel</th><th>Min tot</th><th>Min proy</th><th>Efic.</th><th>Incid.</th></tr></thead>'
        '<tbody>%s</tbody></table>'
        '<p style="font-size:.78rem;color:#64748b;margin-top:.5rem">'
        "Eficiencia = min proyectados (vueltas x min/vuelta; vueltas = piezas / cabezas) / min reales x 100. "
        "Jornada resta comida 14-15. Velada 18:00-02:00. Sabado 8:00-17:30.</p></div>"
    ) % (
        titulo, tabs, periodo, fecha_ref,
        tot_pzas, "{:,}".format(tot_punt), tot_j, tot_v, tot_j + tot_v, tot_inc,
        rows_from(by_op, lambda x: x), rows_from(by_maq, nombre_maq),
    )
    return page("Productividad", body, session)

@app.route("/usuarios")
@login_required
def usuarios_lista():
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin</div>', session)
    body = (
        '<div class="card"><h1>Usuarios</h1>'
        '<table><thead><tr><th>Usuario</th><th>Contrasena inicial</th><th>Rol</th></tr></thead><tbody>'
        "<tr><td>admin</td><td>admin123</td><td>Administrador - todo</td></tr>"
        "<tr><td>gerente</td><td>gerente123</td><td>Gerente - recepcion, asignacion, reportes</td></tr>"
        "<tr><td>operador</td><td>operador123</td><td>Operador - reportar productividad</td></tr>"
        "</tbody></table>"
        '<p style="font-size:.85rem;color:#64748b;margin-top:.8rem">Cambia estas claves en produccion.</p></div>'
    )
    return page("Usuarios", body, session)

@app.errorhandler(500)
def err500(e):
    import traceback
    print("500:", traceback.format_exc())
    msg = str(e) if e else "Error interno"
    body = (
        '<div class="card"><h1>Error temporal</h1>'
        '<div class="alert alert-err">La aplicacion encontro un problema.</div>'
        '<p style="font-size:.85rem;color:#64748b">Detalle: <code>%s</code></p>'
        '<p style="font-size:.85rem">Verifica en Render: Environment - DATABASE_URL (Internal URL de Postgres).</p>'
        '<a class="btn btn-outline" href="/login">Login</a></div>'
    ) % (msg[:300].replace("<", "&lt;"),)
    try:
        return page("Error", body), 500
    except Exception:
        return msg, 500

@app.errorhandler(Exception)
def err_any(e):
    import traceback
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    print("UNCAUGHT:", traceback.format_exc())
    return err500(e)

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
