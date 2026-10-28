#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sistema de Bordado v1"""
from flask import Flask, request, redirect, url_for, session, render_template_string
from functools import wraps
from datetime import datetime, timedelta, time as dtime
import os, hashlib

app = Flask(__name__)
_INIT_DONE = False
APP_VERSION = "2026-10-28-bordado-v1.1-fix500"
app.secret_key = os.environ.get("SECRET_KEY", "clave_secreta_bordado_2026")
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
USE_POSTGRES = bool(DATABASE_URL)
if USE_POSTGRES:
    import psycopg2, psycopg2.extras
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
def get_db():
    if USE_POSTGRES:
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=8, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
        except Exception as e:
            raise RuntimeError("No se pudo conectar a PostgreSQL. Revisa DATABASE_URL en Render. Detalle: %s" % e)
    conn = sqlite3.connect(str(DB_PATH), timeout=8)
    conn.row_factory = sqlite3.Row
    return conn

def dcur(conn):
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()

def ph():
    return "%s" if USE_POSTGRES else "?"

def as_dict(row):
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return {}

def row_get(row, key, default=None):
    d = as_dict(row)
    return d.get(key, default)

def h(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

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
            id SERIAL PRIMARY KEY, vale TEXT DEFAULT '', proyecto TEXT DEFAULT '',
            cliente TEXT DEFAULT '', estado TEXT DEFAULT 'Abierto', observaciones TEXT DEFAULT '',
            creado_por TEXT, creado_en TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS disenos (
            id SERIAL PRIMARY KEY, trabajo_id INT REFERENCES trabajos(id) ON DELETE CASCADE,
            nombre TEXT NOT NULL, puntadas INT DEFAULT 0, minutos_proyectados REAL DEFAULT 0,
            modelo TEXT DEFAULT '', color TEXT DEFAULT '', observaciones TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS registros (
            id SERIAL PRIMARY KEY, trabajo_id INT REFERENCES trabajos(id) ON DELETE CASCADE,
            diseno_id INT, operador TEXT NOT NULL, maquina_id TEXT NOT NULL,
            piezas INT DEFAULT 0, puntadas_unit INT DEFAULT 0, minutos_proyectados REAL DEFAULT 0,
            fecha_inicio TEXT, hora_inicio TEXT, fecha_fin TEXT, hora_fin TEXT,
            observaciones TEXT DEFAULT '', registrado_por TEXT, creado_en TEXT)""")
    else:
        cur.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, nombre TEXT, rol TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS trabajos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vale TEXT DEFAULT '', proyecto TEXT DEFAULT '',
            cliente TEXT DEFAULT '', estado TEXT DEFAULT 'Abierto', observaciones TEXT DEFAULT '',
            creado_por TEXT, creado_en TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS disenos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trabajo_id INTEGER,
            nombre TEXT NOT NULL, puntadas INTEGER DEFAULT 0, minutos_proyectados REAL DEFAULT 0,
            modelo TEXT DEFAULT '', color TEXT DEFAULT '', observaciones TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trabajo_id INTEGER, diseno_id INTEGER,
            operador TEXT NOT NULL, maquina_id TEXT NOT NULL,
            piezas INTEGER DEFAULT 0, puntadas_unit INTEGER DEFAULT 0, minutos_proyectados REAL DEFAULT 0,
            fecha_inicio TEXT, hora_inicio TEXT, fecha_fin TEXT, hora_fin TEXT,
            observaciones TEXT DEFAULT '', registrado_por TEXT, creado_en TEXT)""")
    for u, p, n, r in [("admin", h("admin123"), "Administrador", "admin"),
                       ("gerente", h("gerente123"), "Gerente", "gerente"),
                       ("operador", h("operador123"), "Operador", "operador")]:
        try:
            if USE_POSTGRES:
                cur.execute("INSERT INTO usuarios (username,password_hash,nombre,rol) VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING", (u,p,n,r))
            else:
                cur.execute("INSERT OR IGNORE INTO usuarios (username,password_hash,nombre,rol) VALUES (?,?,?,?)", (u,p,n,r))
        except Exception:
            pass
    try:
        conn.commit()
    except Exception as e:
        print("commit init", e)
        try: conn.rollback()
        except Exception: pass
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
        print("init_db ERROR:", type(e).__name__, e)
        # no re-raise: login puede mostrar mensaje

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
    if not fecha: return None
    hh = (hora or "00:00").strip()
    if len(hh) == 5: hh += ":00"
    try:
        return datetime.strptime(fecha[:10] + " " + hh[:8], "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime(fecha[:10] + " " + hh[:5], "%Y-%m-%d %H:%M")
        except Exception:
            return None

def jornada_del_dia(d):
    if d.weekday() > 5: return None
    if d.weekday() == 0:
        ini, fin = dtime(8, 0), dtime(18, 0)
    else:
        ini, fin = dtime(8, 0), dtime(17, 30)
    return (datetime.combine(d, ini), datetime.combine(d, fin),
            datetime.combine(d, dtime(14, 0)), datetime.combine(d, dtime(15, 0)))

def minutos_en_jornada(t0, t1):
    if not t0 or not t1 or t1 <= t0: return 0
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
                if cb > ca: mins -= (cb - ca).total_seconds() / 60.0
                total += max(0, mins)
        d += timedelta(days=1)
    return int(round(total))

def minutos_en_velada(t0, t1):
    if not t0 or not t1 or t1 <= t0: return 0
    total = 0.0
    d = t0.date() - timedelta(days=1); end_d = t1.date()
    while d <= end_d:
        v0 = datetime.combine(d, dtime(18, 0))
        v1 = datetime.combine(d + timedelta(days=1), dtime(2, 0))
        a, b = max(t0, v0), min(t1, v1)
        if b > a: total += (b - a).total_seconds() / 60.0
        d += timedelta(days=1)
    return int(round(total))

def minutos_trabajados(fi, hi, ff, hf):
    t0, t1 = parse_dt(fi, hi), parse_dt(ff, hf)
    if not t0: return {"jornada": 0, "velada": 0, "total": 0, "abierto": True}
    if not t1: return {"jornada": 0, "velada": 0, "total": 0, "abierto": True}
    j, v = minutos_en_jornada(t0, t1), minutos_en_velada(t0, t1)
    return {"jornada": j, "velada": v, "total": j + v, "abierto": False}

def cabezas_maq(mid): return MAQ_BY_ID.get(mid, {}).get("cabezas", 1)
def nombre_maq(mid): return MAQ_BY_ID.get(mid, {}).get("nombre", mid or "-")


CSS = (
"* {box-sizing:border-box} body{margin:0;font-family:system-ui,sans-serif;background:#f1f5f9;color:#0f172a}"
".nav{background:#0f172a;color:#fff;padding:.65rem 1rem;display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}"
".nav a{color:#e2e8f0;text-decoration:none;font-size:.9rem}.nav .brand{font-weight:700;color:#fff;margin-right:.5rem}"
".container{max-width:1100px;margin:0 auto;padding:1rem}"
".card{background:#fff;border-radius:12px;padding:1rem 1.15rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(0,0,0,.06)}"
"h1{font-size:1.35rem;margin:0 0 .5rem}h2{font-size:1.05rem;margin:0 0 .6rem}"
".btn{display:inline-block;padding:.45rem .8rem;border-radius:8px;border:none;cursor:pointer;font-size:.88rem;text-decoration:none}"
".btn-primary{background:#4f46e5;color:#fff}.btn-success{background:#059669;color:#fff}"
".btn-outline{background:#fff;border:1px solid #cbd5e1;color:#334155}.btn-sm{padding:.3rem .55rem;font-size:.8rem}"
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
".stat .n{font-size:1.4rem;font-weight:700}.stat .l{font-size:.72rem;color:#64748b}"
)

def page(title, body, sess=None):
    sess = sess or session
    nav = ""
    if sess.get("user_id"):
        extra = '<a href="/usuarios">Usuarios</a>' if is_admin_or_gerente() else ""
        nav = (
            '<div class="nav"><span class="brand">Bordado</span>'
            '<a href="/">Inicio</a><a href="/trabajos">Trabajos</a>'
            '<a href="/registro/nuevo">+ Registro</a><a href="/tablero">Tablero</a>'
            '<a href="/productividad">Productividad</a>' + extra
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
        err = "Usuario o contrasena incorrectos"
    body = (
        '<div class="card" style="max-width:380px;margin:2rem auto"><h1>Sistema de Bordado</h1>'
        '<p style="color:#64748b;font-size:.9rem">Vales, maquinas y productividad</p>'
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
    return {"ok": True, "version": APP_VERSION}

@app.route("/")
@login_required
def inicio():
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT COUNT(*) as c FROM trabajos WHERE estado='Abierto'")
    abiertos = row_get(cur.fetchone(), "c", 0) or 0
    hoy = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) as c, COALESCE(SUM(piezas),0) as p FROM registros WHERE fecha_inicio=" + ph(), (hoy,))
    r = as_dict(cur.fetchone())
    cur.execute("SELECT * FROM registros WHERE fecha_inicio=" + ph() + " ORDER BY id DESC LIMIT 30", (hoy,))
    regs = cur.fetchall() or []
    conn.close()
    cards = ""
    for m in MAQUINAS:
        items = [x for x in regs if x.get("maquina_id") == m["id"]][:3]
        if items:
            lines = "".join(
                "<div style='font-size:.82rem;margin:.25rem 0'><strong>%s</strong> - %s pzas - %s-%s</div>"
                % (x.get("operador"), x.get("piezas") or 0, x.get("hora_inicio") or "", x.get("hora_fin") or "...")
                for x in items
            )
        else:
            lines = "<div style='color:#94a3b8;font-size:.82rem'>Sin actividad hoy</div>"
        cards += (
            '<div class="card" style="margin:0"><h2 style="font-size:.95rem">%s</h2>'
            '<div style="font-size:.75rem;color:#64748b;margin-bottom:.4rem">%s cabezas/ciclo</div>%s</div>'
        ) % (m["nombre"], m["cabezas"], lines)
    body = (
        '<div class="card"><h1>Inicio</h1><div class="grid3">'
        '<div class="stat"><div class="n">%s</div><div class="l">Trabajos abiertos</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Registros hoy</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas hoy</div></div></div>'
        '<div style="margin-top:1rem;display:flex;gap:.5rem;flex-wrap:wrap">'
        '<a class="btn btn-primary" href="/trabajo/nuevo">+ Nuevo trabajo</a>'
        '<a class="btn btn-success" href="/registro/nuevo">+ Registro</a>'
        '<a class="btn btn-outline" href="/tablero">Tablero</a>'
        '<a class="btn btn-outline" href="/productividad">Productividad</a></div></div>'
        '<h2 style="margin:.5rem 0">Maquinas</h2>'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.7rem">%s</div>'
    ) % (abiertos, r.get("c") or 0, r.get("p") or 0, cards)
    return page("Inicio", body, session)


@app.route("/trabajos")
@login_required
def trabajos():
    q = (request.args.get("q") or "").strip()
    conn = get_db(); cur = dcur(conn)
    if q:
        like = "%" + q + "%"
        if USE_POSTGRES:
            cur.execute("SELECT * FROM trabajos WHERE vale ILIKE %s OR proyecto ILIKE %s OR cliente ILIKE %s ORDER BY id DESC LIMIT 200", (like, like, like))
        else:
            cur.execute("SELECT * FROM trabajos WHERE vale LIKE ? OR proyecto LIKE ? OR cliente LIKE ? ORDER BY id DESC LIMIT 200", (like, like, like))
    else:
        cur.execute("SELECT * FROM trabajos ORDER BY id DESC LIMIT 200")
    rows = cur.fetchall() or []; conn.close()
    tr = ""
    for t in rows:
        titulo = t.get("vale") or t.get("proyecto") or ("#" + str(t["id"]))
        sub = []
        if t.get("vale") and t.get("proyecto"):
            sub.append(t.get("proyecto"))
        if t.get("cliente"):
            sub.append(t.get("cliente"))
        est = t.get("estado") or "Abierto"
        badge = "badge-ok" if est == "Abierto" else "badge-pend"
        tr += (
            '<tr><td><a href="/trabajo/%s"><strong>%s</strong></a><br><small style="color:#64748b">%s</small></td>'
            '<td><span class="badge %s">%s</span></td><td>%s</td>'
            '<td><a class="btn btn-outline btn-sm" href="/trabajo/%s">Ver</a></td></tr>'
        ) % (t["id"], titulo, " | ".join(sub), badge, est, (t.get("creado_en") or "")[:16], t["id"])
    body = (
        '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem;align-items:center">'
        '<h1 style="margin:0">Trabajos (vales / proyectos)</h1>'
        '<a class="btn btn-primary" href="/trabajo/nuevo">+ Nuevo</a></div>'
        '<form method="GET" style="margin:.7rem 0;display:flex;gap:.4rem">'
        '<input name="q" value="%s" placeholder="Buscar vale, proyecto, cliente" style="max-width:280px">'
        '<button class="btn btn-outline">Buscar</button></form>'
        '<table><thead><tr><th>Trabajo</th><th>Estado</th><th>Creado</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
    ) % (q, tr or "<tr><td colspan=4>Sin trabajos</td></tr>")
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
        obs = (request.form.get("observaciones") or "").strip()
        if not vale and not proyecto:
            err = "Indica al menos Vale o Proyecto"
        else:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = get_db(); cur = dcur(conn)
            if USE_POSTGRES:
                cur.execute(
                    "INSERT INTO trabajos (vale,proyecto,cliente,observaciones,creado_por,creado_en,estado) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'Abierto') RETURNING id",
                    (vale, proyecto, cliente, obs, session.get("username"), now))
                tid = cur.fetchone()["id"]
            else:
                cur.execute(
                    "INSERT INTO trabajos (vale,proyecto,cliente,observaciones,creado_por,creado_en,estado) "
                    "VALUES (?,?,?,?,?,?,'Abierto')",
                    (vale, proyecto, cliente, obs, session.get("username"), now))
                tid = cur.lastrowid
            for i in range(1, 6):
                nom = (request.form.get("d_nombre_%s" % i) or "").strip()
                if not nom:
                    continue
                try:
                    pu = int(request.form.get("d_puntadas_%s" % i) or 0)
                except Exception:
                    pu = 0
                try:
                    mi = float(str(request.form.get("d_mins_%s" % i) or 0).replace(",", "."))
                except Exception:
                    mi = 0
                mod = (request.form.get("d_modelo_%s" % i) or "").strip()
                col = (request.form.get("d_color_%s" % i) or "").strip()
                if USE_POSTGRES:
                    cur.execute(
                        "INSERT INTO disenos (trabajo_id,nombre,puntadas,minutos_proyectados,modelo,color) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (tid, nom, pu, mi, mod, col))
                else:
                    cur.execute(
                        "INSERT INTO disenos (trabajo_id,nombre,puntadas,minutos_proyectados,modelo,color) "
                        "VALUES (?,?,?,?,?,?)", (tid, nom, pu, mi, mod, col))
            conn.commit(); conn.close()
            return redirect(url_for("trabajo_ver", trabajo_id=tid))
    slots = ""
    for i in range(1, 6):
        slots += (
            '<div class="card" style="background:#f8fafc;margin:.4rem 0;padding:.7rem">'
            '<div class="form-row">'
            '<div><label>Diseno %s</label><input name="d_nombre_%s" placeholder="Nombre (opcional)"></div>'
            '<div><label>Puntadas</label><input type="number" name="d_puntadas_%s" value="0" min="0"></div>'
            '<div><label>Min / vuelta</label><input type="number" step="0.1" name="d_mins_%s" value="0" min="0"></div>'
            '<div><label>Modelo</label><input name="d_modelo_%s"></div>'
            '<div><label>Color</label><input name="d_color_%s"></div>'
            '</div></div>'
        ) % (i, i, i, i, i, i)
    body = (
        '<div class="card"><h1>Nuevo trabajo</h1>'
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST"><div class="form-row">'
        '<div><label>Vale de bordado</label><input name="vale" placeholder="Opcional si hay proyecto"></div>'
        '<div><label>Proyecto</label><input name="proyecto" placeholder="Opcional si hay vale"></div>'
        '<div><label>Cliente</label><input name="cliente"></div></div>'
        '<label>Observaciones</label><textarea name="observaciones" rows="2"></textarea>'
        '<h2 style="margin-top:1rem">Disenos (hasta 5; un vale puede tener varios)</h2>'
        + slots
        + '<div style="margin-top:1rem"><button class="btn btn-primary">Guardar trabajo</button> '
        '<a class="btn btn-outline" href="/trabajos">Cancelar</a></div></form></div>'
    )
    return page("Nuevo trabajo", body, session)

@app.route("/trabajo/<int:trabajo_id>")
@login_required
def trabajo_ver(trabajo_id):
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT * FROM trabajos WHERE id=" + ph(), (trabajo_id,))
    t = cur.fetchone()
    if not t:
        conn.close()
        return redirect(url_for("trabajos"))
    cur.execute("SELECT * FROM disenos WHERE trabajo_id=" + ph() + " ORDER BY id", (trabajo_id,))
    disenos = cur.fetchall() or []
    cur.execute("SELECT * FROM registros WHERE trabajo_id=" + ph() + " ORDER BY id DESC LIMIT 100", (trabajo_id,))
    regs = cur.fetchall() or []
    conn.close()
    titulo = t.get("vale") or t.get("proyecto") or ("#" + str(t["id"]))
    drows = "".join(
        "<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (d.get("nombre"), d.get("puntadas") or 0, d.get("minutos_proyectados") or 0, d.get("modelo") or "-", d.get("color") or "-")
        for d in disenos
    )
    rrows = ""
    for r in regs:
        mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"))
        pzas = int(r.get("piezas") or 0)
        pu = int(r.get("puntadas_unit") or 0)
        rrows += (
            "<tr><td>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s / %s</td><td>%s min</td>"
            '<td><a class="btn btn-outline btn-sm" href="/registro/%s">Ver</a></td></tr>'
        ) % (
            r.get("fecha_inicio") or "", r.get("hora_inicio") or "",
            r.get("operador"), nombre_maq(r.get("maquina_id")),
            pzas, "{:,}".format(pu * pzas),
            mt["jornada"], mt["velada"], mt["total"], r["id"],
        )
    body = (
        '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">'
        '<div><h1 style="margin:0">%s</h1>'
        '<div style="font-size:.85rem;color:#64748b">Vale: <strong>%s</strong> | Proyecto: <strong>%s</strong> | Cliente: %s</div>'
        '<span class="badge badge-info">%s</span></div>'
        '<div><a class="btn btn-success" href="/registro/nuevo?trabajo_id=%s">+ Registro</a> '
        '<a class="btn btn-outline" href="/trabajos">Volver</a></div></div>'
        '<p style="font-size:.85rem;margin-top:.5rem">%s</p></div>'
        '<div class="card"><h2>Disenos</h2><table><thead><tr><th>Diseno</th><th>Puntadas</th><th>Min/vuelta</th>'
        '<th>Modelo</th><th>Color</th></tr></thead><tbody>%s</tbody></table></div>'
        '<div class="card"><h2>Registros</h2><table><thead><tr><th>Inicio</th><th>Operador</th><th>Maquina</th>'
        '<th>Piezas</th><th>Puntadas</th><th>Min jor/vel</th><th>Total</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
    ) % (
        titulo, t.get("vale") or "-", t.get("proyecto") or "-", t.get("cliente") or "-",
        t.get("estado"), trabajo_id, t.get("observaciones") or "",
        drows or "<tr><td colspan=5>Sin disenos</td></tr>",
        rrows or "<tr><td colspan=8>Sin registros</td></tr>",
    )
    return page(titulo, body, session)


@app.route("/registro/nuevo", methods=["GET", "POST"])
@login_required
@edit_required
def registro_nuevo():
    err = ""
    trabajo_id = request.args.get("trabajo_id") or request.form.get("trabajo_id") or ""
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT * FROM trabajos WHERE estado='Abierto' ORDER BY id DESC LIMIT 100")
    trabajos_list = cur.fetchall() or []
    disenos = []
    if trabajo_id:
        try:
            cur.execute("SELECT * FROM disenos WHERE trabajo_id=" + ph() + " ORDER BY id", (int(trabajo_id),))
            disenos = cur.fetchall() or []
        except Exception:
            pass
    if request.method == "POST":
        try:
            tid = int(request.form.get("trabajo_id") or 0)
        except Exception:
            tid = 0
        operador = (request.form.get("operador") or "").strip()
        maquina_id = (request.form.get("maquina_id") or "").strip()
        try:
            piezas = int(request.form.get("piezas") or 0)
        except Exception:
            piezas = 0
        try:
            did = int(request.form.get("diseno_id") or 0) or None
        except Exception:
            did = None
        puntadas_unit = 0
        mins_proy = 0.0
        if did:
            cur.execute("SELECT * FROM disenos WHERE id=" + ph(), (did,))
            d = cur.fetchone()
            if d:
                puntadas_unit = int(d.get("puntadas") or 0)
                mins_proy = float(d.get("minutos_proyectados") or 0)
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
                    "INSERT INTO registros (trabajo_id,diseno_id,operador,maquina_id,piezas,puntadas_unit,"
                    "minutos_proyectados,fecha_inicio,hora_inicio,fecha_fin,hora_fin,observaciones,"
                    "registrado_por,creado_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (tid, did, operador, maquina_id, piezas, puntadas_unit, mins_proy,
                     fi, hi, ff or None, hf or None, obs, session.get("username"), now))
                rid = cur.fetchone()["id"]
            else:
                cur.execute(
                    "INSERT INTO registros (trabajo_id,diseno_id,operador,maquina_id,piezas,puntadas_unit,"
                    "minutos_proyectados,fecha_inicio,hora_inicio,fecha_fin,hora_fin,observaciones,"
                    "registrado_por,creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, did, operador, maquina_id, piezas, puntadas_unit, mins_proy,
                     fi, hi, ff or None, hf or None, obs, session.get("username"), now))
                rid = cur.lastrowid
            conn.commit(); conn.close()
            return redirect(url_for("registro_ver", reg_id=rid))
    conn.close()
    opts_t = '<option value="">- Seleccionar -</option>'
    for t in trabajos_list:
        lab = t.get("vale") or t.get("proyecto") or ("#" + str(t["id"]))
        if t.get("vale") and t.get("proyecto"):
            lab = "%s | %s" % (t.get("vale"), t.get("proyecto"))
        sel = " selected" if str(t["id"]) == str(trabajo_id) else ""
        opts_t += '<option value="%s"%s>%s</option>' % (t["id"], sel, lab)
    opts_d = '<option value="">- Manual / sin diseno -</option>'
    for d in disenos:
        opts_d += '<option value="%s">%s (%s puntadas)</option>' % (
            d["id"], d.get("nombre"), d.get("puntadas") or 0)
    opts_m = "".join(
        '<option value="%s">%s (%s cabezas)</option>' % (m["id"], m["nombre"], m["cabezas"])
        for m in MAQUINAS)
    hoy = datetime.now().strftime("%Y-%m-%d")
    ahora = datetime.now().strftime("%H:%M")
    op_val = session.get("nombre") if session.get("rol") == "operador" else ""
    body = (
        '<div class="card"><h1>Nuevo registro de bordado</h1>'
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST"><div class="form-row">'
        '<div><label>Trabajo (vale / proyecto)</label>'
        '<select name="trabajo_id" required onchange="location=\'/registro/nuevo?trabajo_id=\'+this.value">%s</select></div>'
        '<div><label>Diseno</label><select name="diseno_id">%s</select></div></div>'
        '<div class="form-row">'
        '<div><label>Operador</label><input name="operador" required value="%s"></div>'
        '<div><label>Maquina</label><select name="maquina_id" required>%s</select></div>'
        '<div><label>Piezas</label><input type="number" name="piezas" min="0" value="0" required></div></div>'
        '<div class="form-row">'
        '<div><label>Puntadas por bordado</label><input type="number" name="puntadas_unit" min="0" placeholder="Auto si eliges diseno"></div>'
        '<div><label>Min proyectados / vuelta</label><input type="number" step="0.1" name="minutos_proyectados" min="0"></div></div>'
        '<div class="form-row">'
        '<div><label>Fecha inicio</label><input type="date" name="fecha_inicio" value="%s" required></div>'
        '<div><label>Hora inicio</label><input type="time" name="hora_inicio" value="%s" required></div>'
        '<div><label>Fecha fin</label><input type="date" name="fecha_fin"></div>'
        '<div><label>Hora fin</label><input type="time" name="hora_fin"></div></div>'
        '<p style="font-size:.78rem;color:#64748b">Puedes dejar fin vacio y completarlo despues. Velada 18:00-02:00. Sabado 8:00-17:30.</p>'
        '<label>Observaciones</label><textarea name="observaciones" rows="2"></textarea>'
        '<div style="margin-top:.8rem"><button class="btn btn-primary">Guardar</button> '
        '<a class="btn btn-outline" href="/">Cancelar</a></div></form></div>'
    ) % (opts_t, opts_d, op_val, opts_m, hoy, ahora)
    return page("Nuevo registro", body, session)

@app.route("/registro/<int:reg_id>", methods=["GET", "POST"])
@login_required
def registro_ver(reg_id):
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT * FROM registros WHERE id=" + ph(), (reg_id,))
    r = cur.fetchone()
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
        obs = (request.form.get("observaciones") or "").strip()
        if USE_POSTGRES:
            cur.execute("UPDATE registros SET fecha_fin=%s,hora_fin=%s,piezas=%s,observaciones=%s WHERE id=%s",
                        (ff or None, hf or None, piezas, obs, reg_id))
        else:
            cur.execute("UPDATE registros SET fecha_fin=?,hora_fin=?,piezas=?,observaciones=? WHERE id=?",
                        (ff or None, hf or None, piezas, obs, reg_id))
        conn.commit()
        cur.execute("SELECT * FROM registros WHERE id=" + ph(), (reg_id,))
        r = cur.fetchone()
    cur.execute("SELECT * FROM trabajos WHERE id=" + ph(), (r.get("trabajo_id"),))
    t = cur.fetchone()
    conn.close()
    mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"))
    cab = cabezas_maq(r.get("maquina_id"))
    pzas = int(r.get("piezas") or 0)
    pu = int(r.get("puntadas_unit") or 0)
    mins_proy = float(r.get("minutos_proyectados") or 0)
    vueltas = (pzas / cab) if cab else pzas
    proy_total = vueltas * mins_proy
    eff = (proy_total / mt["total"] * 100) if mt["total"] > 0 and proy_total > 0 else None
    titulo = (t.get("vale") or t.get("proyecto") or "") if t else ""
    form_edit = ""
    if can_edit():
        form_edit = (
            '<form method="POST" class="card"><h2>Actualizar / cerrar</h2><div class="form-row">'
            '<div><label>Piezas</label><input type="number" name="piezas" value="%s" min="0"></div>'
            '<div><label>Fecha fin</label><input type="date" name="fecha_fin" value="%s"></div>'
            '<div><label>Hora fin</label><input type="time" name="hora_fin" value="%s"></div></div>'
            '<label>Observaciones</label><textarea name="observaciones" rows="2">%s</textarea>'
            '<button class="btn btn-primary" style="margin-top:.5rem">Guardar</button></form>'
        ) % (pzas, r.get("fecha_fin") or "", (r.get("hora_fin") or "")[:5], r.get("observaciones") or "")
    body = (
        '<div class="card"><h1>Registro #%s</h1>'
        '<p>Trabajo: <a href="/trabajo/%s"><strong>%s</strong></a></p>'
        '<div class="grid3">'
        '<div class="stat"><div class="n">%s</div><div class="l">Operador</div></div>'
        '<div class="stat"><div class="n" style="font-size:1rem">%s</div><div class="l">%s cabezas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas tot.</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min jornada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min velada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min totales</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min proyectados</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Eficiencia</div></div>'
        '</div>'
        '<p style="font-size:.85rem;margin-top:.7rem;color:#64748b">Inicio: <strong>%s %s</strong> | Fin: <strong>%s %s</strong>%s</p></div>'
        '%s<a class="btn btn-outline" href="/trabajo/%s">Volver al trabajo</a>'
    ) % (
        reg_id, r.get("trabajo_id"), titulo or r.get("trabajo_id"),
        r.get("operador"), nombre_maq(r.get("maquina_id")), cab, pzas, "{:,}".format(pu * pzas),
        mt["jornada"], mt["velada"], mt["total"],
        ("%.0f" % proy_total) if proy_total else "-",
        ("%.0f%%" % eff) if eff is not None else "-",
        r.get("fecha_inicio") or "", r.get("hora_inicio") or "",
        r.get("fecha_fin") or "-", r.get("hora_fin") or "",
        ' <span class="badge badge-warn">En proceso</span>' if mt["abierto"] else "",
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
    regs = cur.fetchall() or []; conn.close()
    cards = ""
    for m in MAQUINAS:
        items = [x for x in regs if x.get("maquina_id") == m["id"]]
        abiertos = [x for x in items if not (x.get("fecha_fin") and x.get("hora_fin"))]
        cerrados = [x for x in items if x.get("fecha_fin") and x.get("hora_fin")]
        lista = abiertos + cerrados[:5]
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
    body = (
        '<div class="card"><h1>Tablero de maquinas</h1>'
        '<p style="color:#64748b;font-size:.9rem">Actividad de hoy (%s)</p></div>%s'
    ) % (hoy, cards)
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
    regs = cur.fetchall() or []; conn.close()
    by_op, by_maq = {}, {}
    tot_pzas = tot_punt = tot_j = tot_v = 0
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
        for bucket, key in ((by_op, op), (by_maq, mid)):
            st = bucket.setdefault(key, {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "proy": 0.0, "regs": 0})
            st["pzas"] += pzas; st["punt"] += punt
            st["jornada"] += mt["jornada"]; st["velada"] += mt["velada"]; st["total"] += mt["total"]
            st["proy"] += proy; st["regs"] += 1
        tot_pzas += pzas; tot_punt += punt; tot_j += mt["jornada"]; tot_v += mt["velada"]

    def rows_from(bucket, label_fn):
        out = ""
        for k, st in sorted(bucket.items(), key=lambda x: -x[1]["pzas"]):
            eff = (st["proy"] / st["total"] * 100) if st["total"] and st["proy"] else None
            out += (
                "<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%.0f</td><td>%s</td></tr>"
            ) % (
                label_fn(k), st["regs"], st["pzas"], "{:,}".format(st["punt"]),
                st["jornada"], st["velada"], st["total"], st["proy"],
                ("%.0f%%" % eff) if eff is not None else "-",
            )
        return out or "<tr><td colspan=9>Sin datos</td></tr>"

    tabs = ""
    for key, lab in [("dia", "Dia"), ("semana", "Semana"), ("mes", "Mes")]:
        cls = "btn-primary" if periodo == key else "btn-outline"
        tabs += '<a class="btn %s btn-sm" href="/productividad?periodo=%s&fecha=%s">%s</a> ' % (cls, key, fecha_ref, lab)
    body = (
        '<div class="card"><h1>Productividad</h1><p style="color:#64748b">%s</p>'
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
        '<div class="stat"><div class="n">%s</div><div class="l">Min totales</div></div></div></div>'
        '<div class="card"><h2>Por operador</h2>'
        '<table style="font-size:.82rem"><thead><tr><th>Operador</th><th>Regs</th><th>Piezas</th><th>Puntadas</th>'
        '<th>Min jor</th><th>Min vel</th><th>Min tot</th><th>Min proy</th><th>Eficiencia</th></tr></thead>'
        '<tbody>%s</tbody></table></div>'
        '<div class="card"><h2>Por maquina</h2>'
        '<table style="font-size:.82rem"><thead><tr><th>Maquina</th><th>Regs</th><th>Piezas</th><th>Puntadas</th>'
        '<th>Min jor</th><th>Min vel</th><th>Min tot</th><th>Min proy</th><th>Eficiencia</th></tr></thead>'
        '<tbody>%s</tbody></table>'
        '<p style="font-size:.78rem;color:#64748b;margin-top:.5rem">'
        "Eficiencia = min proyectados (vueltas x min/vuelta; vueltas = piezas / cabezas) / min reales x 100. "
        "Jornada resta comida 14-15. Velada 18:00-02:00. Sabado 8:00-17:30.</p></div>"
    ) % (
        titulo, tabs, periodo, fecha_ref,
        tot_pzas, "{:,}".format(tot_punt), tot_j, tot_v, tot_j + tot_v,
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
        "<tr><td>admin</td><td>admin123</td><td>Administrador</td></tr>"
        "<tr><td>gerente</td><td>gerente123</td><td>Gerente</td></tr>"
        "<tr><td>operador</td><td>operador123</td><td>Operador (captura)</td></tr>"
        "</tbody></table>"
        '<p style="font-size:.85rem;color:#64748b;margin-top:.8rem">Cambia estas claves cuando el equipo empiece a usar el sistema.</p></div>'
    )
    return page("Usuarios", body, session)

@app.errorhandler(500)
def err500(e):
    import traceback
    tb = traceback.format_exc()
    print("500 ERROR:", tb)
    msg = str(e) if e else "Error interno"
    body = (
        '<div class="card"><h1>Error temporal</h1>'
        '<div class="alert alert-err">La aplicacion encontro un problema.</div>'
        '<p style="font-size:.85rem;color:#64748b">Detalle (para soporte): <code>%s</code></p>'
        '<p style="font-size:.85rem">Revisa en Render: Environment -&gt; DATABASE_URL (Internal Database URL de tu Postgres).</p>'
        '<a class="btn btn-outline" href="/login">Ir al login</a></div>'
    ) % (msg[:300].replace("<", "&lt;"),)
    try:
        return page("Error", body), 500
    except Exception:
        return msg, 500

@app.errorhandler(Exception)
def err_any(e):
    import traceback
    print("UNCAUGHT:", traceback.format_exc())
    # dejar que 404 etc pasen
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    return err500(e)


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
