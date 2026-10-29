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
try:
    from zoneinfo import ZoneInfo
    TZ_MX = ZoneInfo("America/Mexico_City")
except Exception:
    TZ_MX = None

def now_mx():
    if TZ_MX is not None:
        return datetime.now(TZ_MX).replace(tzinfo=None)
    return datetime.now() - timedelta(hours=6)  # fallback CST approx

def today_mx():
    return now_mx().strftime("%Y-%m-%d")

def now_str_mx():
    return now_mx().strftime("%Y-%m-%d %H:%M")

def now_time_mx():
    return now_mx().strftime("%H:%M")


# --- Jornada bordado (Ciudad de Mexico) ---
# Lun-Vie: 08:00 inicio, comida 13:00-14:00 (no disponible)
# Lunes salida 17:30 | Mar-Vie salida 18:00
# Sabado / velada: SOLO si gerencia autoriza en la asignacion

def es_dia_laboral(d, permite_sabado=False):
    if d.weekday() <= 4:
        return True
    if d.weekday() == 5 and permite_sabado:
        return True
    return False

def jornada_bordado(d, permite_sabado=False):
    """Devuelve (inicio_min, fin_min, comida_ini, comida_fin) en minutos desde medianoche, o None."""
    if d.weekday() > 5:
        return None
    if d.weekday() == 5 and not permite_sabado:
        return None
    if d.weekday() > 5:
        return None
    ini = 8 * 60  # 08:00
    comida_i, comida_f = 13 * 60, 14 * 60  # 13:00-14:00
    if d.weekday() == 0:  # lunes
        fin = 17 * 60 + 30  # 17:30
    elif d.weekday() <= 4:
        fin = 18 * 60  # 18:00
    else:  # sabado autorizado
        fin = 17 * 60 + 30  # 17:30 como mar-vie corto
    return ini, fin, comida_i, comida_f

def minutos_disponibles_dia(d, permite_sabado=False):
    j = jornada_bordado(d, permite_sabado)
    if not j:
        return 0
    ini, fin, ci, cf = j
    return max(0, (fin - ini) - (cf - ci))

def siguiente_dia_laboral(d, permite_sabado=False):
    x = d + timedelta(days=1)
    for _ in range(14):
        if es_dia_laboral(x, permite_sabado):
            return x
        x += timedelta(days=1)
    return x

def empaquetar_en_jornada(fecha_date, hora_min, dur_min, permite_sabado=False, velada_fechas=None):
    """Empaqueta dur_min en jornada + velada opcional.
    Jornada: Lun-Vie (Sab si permite), comida 13-14, lun fin 17:30, mar-vie 18:00.
    Velada (solo fechas en velada_fechas): 18:00-02:00, comida 22:00-23:00.
    Devuelve segmentos (date, start_min, end_min) con min 0-1439 del dia del segmento.
    """
    if dur_min <= 0:
        dur_min = 30
    velada_fechas = set(velada_fechas or [])
    d = fecha_date
    cursor = max(0, int(hora_min))
    restantes = float(dur_min)
    segs = []
    guard = 0
    while restantes > 0.5 and guard < 400:
        guard += 1
        if not es_dia_laboral(d, permite_sabado) and d.strftime("%Y-%m-%d") not in velada_fechas:
            # si solo hay velada ese dia raro, igual avanzar
            d = siguiente_dia_laboral(d, permite_sabado)
            cursor = 8 * 60
            continue
        # --- jornada diurna ---
        j = jornada_bordado(d, permite_sabado)
        if j and cursor < j[1]:
            ini, fin, ci, cf = j
            if cursor < ini:
                cursor = ini
            # saltar comida
            if ci <= cursor < cf:
                cursor = cf
            while restantes > 0.5 and cursor < fin:
                if ci <= cursor < cf:
                    cursor = cf
                    if cursor >= fin:
                        break
                hasta = ci if cursor < ci else fin
                caben = hasta - cursor
                if caben <= 0:
                    break
                usa = min(restantes, caben)
                segs.append((d, int(cursor), int(cursor + usa)))
                restantes -= usa
                cursor += usa
                if ci <= cursor < cf:
                    cursor = cf
        # --- velada ese dia calendario (tarde) si autorizada ---
        ds = d.strftime("%Y-%m-%d")
        if restantes > 0.5 and ds in velada_fechas:
            # 18:00 - 24:00 (con comida 22-23)
            v_ini, v_mid, v_food_i, v_food_f, v_end_next = 18 * 60, 24 * 60, 22 * 60, 23 * 60, 2 * 60
            if cursor < v_ini:
                cursor = v_ini
            # tarde del mismo dia hasta medianoche
            while restantes > 0.5 and cursor < v_mid:
                if v_food_i <= cursor < v_food_f:
                    cursor = v_food_f
                if cursor >= v_mid:
                    break
                hasta = v_food_i if cursor < v_food_i else v_mid
                caben = hasta - cursor
                if caben <= 0:
                    cursor = max(cursor, v_food_f)
                    continue
                usa = min(restantes, caben)
                segs.append((d, int(cursor), int(cursor + usa)))
                restantes -= usa
                cursor += usa
            # madrugada dia siguiente 00:00-02:00
            if restantes > 0.5:
                d2 = d + timedelta(days=1)
                cursor2 = 0
                while restantes > 0.5 and cursor2 < v_end_next:
                    # no hay comida en madrugada (comida fue 22-23)
                    caben = v_end_next - cursor2
                    usa = min(restantes, caben)
                    segs.append((d2, int(cursor2), int(cursor2 + usa)))
                    restantes -= usa
                    cursor2 += usa
                d = d2
                cursor = max(cursor2, 8 * 60)  # siguiente jornada
                continue
        # siguiente dia laboral
        d = siguiente_dia_laboral(d, permite_sabado)
        cursor = 8 * 60
    return segs


def min_to_hhmm(m):
    m = int(m) % (24 * 60)
    return "%02d:%02d" % (m // 60, m % 60)


app = Flask(__name__)
application = app  # alias para gunicorn/wsgi
_INIT_DONE = False
APP_VERSION = "2026-10-28-bordado-v3.8-gantt-excel"
app.secret_key = os.environ.get("SECRET_KEY", "clave_secreta_bordado_v3_2026")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
USE_POSTGRES = bool(DATABASE_URL)
print("BORDADO DB:", "POSTGRES" if USE_POSTGRES else "SQLITE (DATOS TEMPORALES - configura DATABASE_URL)")

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
            minutos_plan REAL DEFAULT 0,
            fecha_plan TEXT DEFAULT '',
            hora_inicio_plan TEXT DEFAULT '08:00',
            sabado_autorizado INT DEFAULT 0,
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
        cur.execute("""CREATE TABLE IF NOT EXISTS ponchados (
            id SERIAL PRIMARY KEY,
            logo TEXT NOT NULL,
            cliente TEXT DEFAULT '',
            puntadas INT DEFAULT 0,
            fecha_inicio TEXT, hora_inicio TEXT,
            fecha_fin TEXT, hora_fin TEXT,
            usuario TEXT,
            observaciones TEXT DEFAULT '',
            creado_en TEXT)""")
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
            piezas_objetivo INTEGER DEFAULT 0, minutos_plan REAL DEFAULT 0,
            fecha_plan TEXT DEFAULT '', hora_inicio_plan TEXT DEFAULT '08:00', sabado_autorizado INTEGER DEFAULT 0,
            estado TEXT DEFAULT 'Pendiente',
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
        cur.execute("""CREATE TABLE IF NOT EXISTS ponchados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logo TEXT NOT NULL,
            cliente TEXT DEFAULT '',
            puntadas INTEGER DEFAULT 0,
            fecha_inicio TEXT, hora_inicio TEXT,
            fecha_fin TEXT, hora_fin TEXT,
            usuario TEXT,
            observaciones TEXT DEFAULT '',
            creado_en TEXT)""")

    for sql_pg, sql_sq in [
        ("ALTER TABLE registros ADD COLUMN IF NOT EXISTS velada_autorizada INT DEFAULT 0",
         "ALTER TABLE registros ADD COLUMN velada_autorizada INTEGER DEFAULT 0"),
        ("ALTER TABLE ponchados ADD COLUMN IF NOT EXISTS velada_autorizada INT DEFAULT 0",
         "ALTER TABLE ponchados ADD COLUMN velada_autorizada INTEGER DEFAULT 0"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS minutos_plan REAL DEFAULT 0",
         "ALTER TABLE asignaciones ADD COLUMN minutos_plan REAL DEFAULT 0"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS fecha_plan TEXT DEFAULT ''",
         "ALTER TABLE asignaciones ADD COLUMN fecha_plan TEXT DEFAULT ''"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS hora_inicio_plan TEXT DEFAULT '08:00'",
         "ALTER TABLE asignaciones ADD COLUMN hora_inicio_plan TEXT DEFAULT '08:00'"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS sabado_autorizado INT DEFAULT 0",
         "ALTER TABLE asignaciones ADD COLUMN sabado_autorizado INTEGER DEFAULT 0"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS fecha_fin_plan TEXT DEFAULT ''",
         "ALTER TABLE asignaciones ADD COLUMN fecha_fin_plan TEXT DEFAULT ''"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS hora_fin_plan TEXT DEFAULT ''",
         "ALTER TABLE asignaciones ADD COLUMN hora_fin_plan TEXT DEFAULT ''"),
        ("ALTER TABLE asignaciones ADD COLUMN IF NOT EXISTS velada_fechas TEXT DEFAULT ''",
         "ALTER TABLE asignaciones ADD COLUMN velada_fechas TEXT DEFAULT ''"),
    ]:
        try:
            if USE_POSTGRES:
                cur.execute(sql_pg)
            else:
                try:
                    cur.execute(sql_sq)
                except Exception:
                    pass
        except Exception as e:
            print("migrate", e)

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
    now = now_str_mx()
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

def minutos_trabajados(fi, hi, ff, hf, velada_autorizada=False):
    """Velada solo cuenta si fue autorizada (gerente/admin)."""
    t0, t1 = parse_dt(fi, hi), parse_dt(ff, hf)
    if not t0:
        return {"jornada": 0, "velada": 0, "total": 0, "abierto": True}
    if not t1:
        return {"jornada": 0, "velada": 0, "total": 0, "abierto": True}
    j = minutos_en_jornada(t0, t1)
    v = minutos_en_velada(t0, t1) if velada_autorizada else 0
    return {"jornada": j, "velada": v, "total": j + v, "abierto": False}

def velada_flag(row):
    if not row:
        return False
    try:
        v = row.get("velada_autorizada") if hasattr(row, "get") else row["velada_autorizada"]
    except Exception:
        v = 0
    return bool(v) and str(v) not in ("0", "False", "false", "None", "")

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
            '<a href="/tablero">Tablero</a><a href="/cola">Cola/Gantt</a><a href="/cola/lista">Cola lista</a><a href="/ponchados">Ponchados</a><a href="/operador">Por operador</a><a href="/productividad">Productividad</a>'
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
    hoy = today_mx()
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
        recibido_fecha = (request.form.get("recibido_fecha") or "").strip() or today_mx()
        try:
            piezas_rec = int(request.form.get("piezas_recibidas") or 0)
        except Exception:
            piezas_rec = 0
        obs_rec = (request.form.get("obs_recepcion") or "").strip()
        obs = (request.form.get("observaciones") or "").strip()
        if not vale and not proyecto:
            err = "Indica al menos Vale o Proyecto"
        else:
            now = now_str_mx()
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
    hoy = today_mx()
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
        pv = plan_ventana(a.get("fecha_plan"), a.get("hora_inicio_plan"),
                          a.get("minutos_plan"), a.get("sabado_autorizado"), a.get("velada_fechas"))
        arows += (
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%.0f min</td>"
            "<td>%s %s → %s %s</td><td>%s</td>"
            '<td><a class="btn btn-success btn-sm" href="/registro/nuevo?asignacion_id=%s">Reportar</a> '
            '<a class="btn btn-outline btn-sm" href="/asignacion/%s/dividir">Dividir</a></td></tr>'
        ) % (
            a.get("operador"), nombre_maq(a.get("maquina_id")) if a.get("maquina_id") else "-",
            a.get("piezas_objetivo") or 0, float(a.get("minutos_plan") or 0),
            pv["fecha_ini"], pv["hora_ini"],
            a.get("fecha_fin_plan") or pv["fecha_fin"],
            (a.get("hora_fin_plan") or pv["hora_fin"] or "")[:5],
            badge_estado(a.get("estado")), a["id"], a["id"],
        )

    rrows = ""
    for r in regs:
        mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"), velada_flag(r))
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
            '<p style="font-size:.82rem;color:#64748b">Reparto multi-operador. Tiempo plan = piezas/cabezas x min del bordado (editable). '
            'Hora base de cola: 08:00 (Ciudad de Mexico).</p>'
            '<form method="POST" action="/trabajo/%s/asignar"><div class="form-row">'
            '<div><label>Operador</label><input name="operador" required placeholder="Nombre"></div>'
            '<div><label>Maquina</label><select name="maquina_id"><option value="">- Opcional -</option>%s</select></div>'
            '<div><label>Bordado</label><select name="bordado_id">%s</select></div>'
            '<div><label>Piezas objetivo</label><input type="number" name="piezas_objetivo" min="0" value="0"></div>'
            '<div><label>Minutos plan (auto si vacio)</label><input type="number" step="1" name="minutos_plan" min="0" placeholder="Auto"></div></div>'
            '<p style="font-size:.78rem;color:#64748b">La cola acomoda sola: Lun-Vie 08:00, comida 13:00-14:00, '
            "salida lun 17:30 / mar-vie 18:00. Si no cabe hoy, pasa al siguiente dia laboral.</p>"
            '<label style="margin-top:.3rem"><input type="checkbox" name="sabado_autorizado" value="1"> '
            "Autorizar sabado (solo gerencia)</label>"
            '<div style="margin-top:.5rem;padding:.6rem;background:#f8fafc;border-radius:8px">'
            '<strong style="font-size:.85rem">Veladas (6pm-2am, comida 10pm-11pm)</strong>'
            '<p style="font-size:.75rem;color:#64748b;margin:.2rem 0">Solo los dias que indiques. Puedes poner varios.</p>'
            '<div class="form-row">'
            '<div><label>Velada fecha 1</label><input type="date" name="velada_fecha"></div>'
            '<div><label>Velada fecha 2</label><input type="date" name="velada_fecha"></div>'
            '<div><label>Velada fecha 3</label><input type="date" name="velada_fecha"></div>'
            '<div><label>Velada fecha 4</label><input type="date" name="velada_fecha"></div></div></div>'
            '<label>Obs.</label><textarea name="observaciones" rows="1"></textarea>'
            '<div style="margin-top:.4rem"><button class="btn btn-primary btn-sm">Asignar a la cola</button> '
            '<a class="btn btn-outline btn-sm" href="/cola?fecha=%s">Ver cola / Gantt</a></div></form></div>'
        ) % (trabajo_id, opts_m, opts_b, today_mx())

    body = (
        '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem">'
        '<div><h1 style="margin:0">%s</h1>'
        '<div style="font-size:.85rem;color:#64748b">Vale: <strong>%s</strong> | Proyecto: <strong>%s</strong> | Cliente: %s</div>'
        '%s</div>'
        '<div><a class="btn btn-success" href="/registro/nuevo?trabajo_id=%s">+ Reportar</a> '
        '<a class="btn btn-outline" href="/trabajos">Volver</a>'
        + ('<form method="POST" action="/trabajo/%s/eliminar" style="display:inline;margin-left:.3rem" onsubmit="return confirm(\'Eliminar este trabajo y todos sus registros?\')"><button class="btn btn-danger btn-sm">Eliminar</button></form>' % (trabajo_id,) if session.get('rol')=='admin' else '')
        + '</div></div>'
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
        '<table><thead><tr><th>Operador</th><th>Maquina</th><th>Pzas obj.</th><th>Min plan</th><th>Plan inicio → fin</th><th>Estado</th><th></th></tr></thead>'
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
    sabado_auth = 1 if request.form.get("sabado_autorizado") else 0
    fecha_plan_in = (request.form.get("fecha_plan") or "").strip()
    hora_inicio_in = (request.form.get("hora_inicio_plan") or "").strip()
    mins_manual = (request.form.get("minutos_plan") or "").strip()
    minutos_plan = 0.0
    if mins_manual:
        try:
            minutos_plan = float(str(mins_manual).replace(",", "."))
        except Exception:
            minutos_plan = 0.0
    conn = get_db(); cur = dcur(conn)
    if minutos_plan <= 0 and bordado_id and pzas > 0:
        cur.execute("SELECT * FROM bordados WHERE id=" + ph(), (bordado_id,))
        b = as_dict(cur.fetchone())
        if b:
            cab = cabezas_maq(maquina_id) if maquina_id else 1
            mins_v = float(b.get("minutos_proyectados") or 0)
            minutos_plan = (pzas / max(cab, 1)) * mins_v
    if not operador:
        conn.close()
        return redirect(url_for("trabajo_ver", trabajo_id=trabajo_id))

    # Cola automatica: acomodar tras lo ya asignado al mismo operador (y dia laboral)
    permite_sab = bool(sabado_auth)
    # cargar asignaciones pendientes/en proceso del operador
    if USE_POSTGRES:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=%s AND estado IN ('Pendiente','En proceso') "
            "ORDER BY fecha_plan, hora_inicio_plan, id", (operador,))
    else:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=? AND estado IN ('Pendiente','En proceso') "
            "ORDER BY fecha_plan, hora_inicio_plan, id", (operador,))
    prev = as_dicts(cur.fetchall())
    # simular fin de cola
    if fecha_plan_in and hora_inicio_in and request.form.get("forzar_horario"):
        try:
            d0 = datetime.strptime(fecha_plan_in[:10], "%Y-%m-%d").date()
        except Exception:
            d0 = now_mx().date()
        try:
            parts = hora_inicio_in.split(":")
            h0 = int(parts[0]) * 60 + int(parts[1])
        except Exception:
            h0 = 8 * 60
    else:
        # empezar hoy 8:00 o al terminar la ultima asignacion del operador
        d0 = now_mx().date()
        if not es_dia_laboral(d0, permite_sab):
            d0 = siguiente_dia_laboral(d0, permite_sab)
        h0 = 8 * 60
        for a in prev:
            fp = (a.get("fecha_plan") or "")[:10]
            if not fp:
                continue
            try:
                ad = datetime.strptime(fp, "%Y-%m-%d").date()
            except Exception:
                continue
            amp = bool(a.get("sabado_autorizado"))
            segs = empaquetar_en_jornada(
                ad, min_of_hhmm(a.get("hora_inicio_plan") or "08:00"),
                float(a.get("minutos_plan") or 0) or 30, amp or permite_sab)
            if segs:
                last = segs[-1]
                if last[0] > d0 or (last[0] == d0 and last[2] > h0):
                    d0, h0 = last[0], last[2]
        # si el cursor quedo en/pasado fin de jornada, pasar al siguiente
        j = jornada_bordado(d0, permite_sab)
        if j and h0 >= j[1]:
            d0 = siguiente_dia_laboral(d0, permite_sab)
            h0 = 8 * 60
        if j and j[2] <= h0 < j[3]:
            h0 = j[3]
    velada_fechas_list = parse_velada_fechas(request.form.get("velada_fechas") or "")
    # tambien aceptar varios inputs name=velada_fecha
    for vf in request.form.getlist("velada_fecha"):
        vf = (vf or "").strip()[:10]
        if vf and vf not in velada_fechas_list:
            velada_fechas_list.append(vf)
    velada_fechas_str = ",".join(velada_fechas_list)
    segs_new = empaquetar_en_jornada(d0, h0, minutos_plan or 30, permite_sab, velada_fechas_list)
    if segs_new:
        fecha_plan = segs_new[0][0].strftime("%Y-%m-%d")
        hora_inicio_plan = min_to_hhmm(segs_new[0][1])
        fecha_fin_plan = segs_new[-1][0].strftime("%Y-%m-%d")
        hora_fin_plan = min_to_hhmm(segs_new[-1][2])
    else:
        fecha_plan = fecha_plan_in or today_mx()
        hora_inicio_plan = hora_inicio_in or "08:00"
        fecha_fin_plan = fecha_plan
        hora_fin_plan = hora_inicio_plan

    now = now_str_mx()
    if USE_POSTGRES:
        cur.execute(
            "INSERT INTO asignaciones (trabajo_id,bordado_id,operador,maquina_id,piezas_objetivo,minutos_plan,"
            "fecha_plan,hora_inicio_plan,fecha_fin_plan,hora_fin_plan,sabado_autorizado,velada_fechas,"
            "estado,fecha_asignacion,asignado_por,observaciones) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pendiente',%s,%s,%s)",
            (trabajo_id, bordado_id, operador, maquina_id, pzas, minutos_plan,
             fecha_plan, hora_inicio_plan, fecha_fin_plan, hora_fin_plan, sabado_auth, velada_fechas_str,
             now, session.get("username"), obs))
        cur.execute("UPDATE trabajos SET estado=%s WHERE id=%s AND estado IN ('Recibido','Asignado')",
                    ("Asignado", trabajo_id))
    else:
        cur.execute(
            "INSERT INTO asignaciones (trabajo_id,bordado_id,operador,maquina_id,piezas_objetivo,minutos_plan,"
            "fecha_plan,hora_inicio_plan,fecha_fin_plan,hora_fin_plan,sabado_autorizado,velada_fechas,"
            "estado,fecha_asignacion,asignado_por,observaciones) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'Pendiente',?,?,?)",
            (trabajo_id, bordado_id, operador, maquina_id, pzas, minutos_plan,
             fecha_plan, hora_inicio_plan, fecha_fin_plan, hora_fin_plan, sabado_auth, velada_fechas_str,
             now, session.get("username"), obs))
        cur.execute("UPDATE trabajos SET estado=? WHERE id=? AND estado IN ('Recibido','Asignado')",
                    ("Asignado", trabajo_id))
    log_evento(cur, trabajo_id, "Asignacion",
               "%s | %s | %s pzas | %.0f min | %s %s%s" % (
                   operador, nombre_maq(maquina_id) if maquina_id else "-", pzas, minutos_plan,
                   fecha_plan, hora_inicio_plan, " | SABADO auth" if sabado_auth else ""))
    conn.commit(); conn.close()
    return redirect(url_for("trabajo_ver", trabajo_id=trabajo_id))

def min_of_hhmm(hhmm):
    try:
        parts = (hhmm or "08:00").split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return 8 * 60



def repack_operador(cur, operador, desde_fecha=None):
    """Reordena asignaciones Pendiente/En proceso del operador empaquetando jornada real."""
    if not operador:
        return
    if USE_POSTGRES:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=%s AND estado IN ('Pendiente','En proceso') "
            "ORDER BY fecha_plan, hora_inicio_plan, id", (operador,))
    else:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=? AND estado IN ('Pendiente','En proceso') "
            "ORDER BY fecha_plan, hora_inicio_plan, id", (operador,))
    rows = as_dicts(cur.fetchall())
    if not rows:
        return
    # respetar orden actual de la lista (ya ordenada); empaquetar en secuencia
    try:
        if desde_fecha:
            d0 = datetime.strptime(desde_fecha[:10], "%Y-%m-%d").date()
        else:
            d0 = now_mx().date()
    except Exception:
        d0 = now_mx().date()
    if not es_dia_laboral(d0, False):
        d0 = siguiente_dia_laboral(d0, False)
    cursor_d, cursor_h = d0, 8 * 60
    for a in rows:
        permite = bool(a.get("sabado_autorizado"))
        dur = float(a.get("minutos_plan") or 0) or 30
        vf = parse_velada_fechas(a.get("velada_fechas"))
        segs = empaquetar_en_jornada(cursor_d, cursor_h, dur, permite, vf)
        if not segs:
            continue
        fp = segs[0][0].strftime("%Y-%m-%d")
        hi = min_to_hhmm(segs[0][1])
        ff = segs[-1][0].strftime("%Y-%m-%d")
        hf = min_to_hhmm(segs[-1][2])
        if USE_POSTGRES:
            cur.execute(
                "UPDATE asignaciones SET fecha_plan=%s, hora_inicio_plan=%s, fecha_fin_plan=%s, hora_fin_plan=%s WHERE id=%s",
                (fp, hi, ff, hf, a["id"]))
        else:
            cur.execute(
                "UPDATE asignaciones SET fecha_plan=?, hora_inicio_plan=?, fecha_fin_plan=?, hora_fin_plan=? WHERE id=?",
                (fp, hi, ff, hf, a["id"]))
        last = segs[-1]
        cursor_d, cursor_h = last[0], last[2]
        j = jornada_bordado(cursor_d, permite)
        if j and cursor_h >= j[1]:
            cursor_d = siguiente_dia_laboral(cursor_d, permite)
            cursor_h = 8 * 60
        elif j and j[2] <= cursor_h < j[3]:
            cursor_h = j[3]



def parse_velada_fechas(val):
    if not val:
        return []
    if isinstance(val, (list, set, tuple)):
        return [str(x)[:10] for x in val]
    return [x.strip()[:10] for x in str(val).replace(";", ",").split(",") if x.strip()]

def plan_ventana(fecha_plan, hora_inicio_plan, minutos_plan, sabado_autorizado=False, velada_fechas=None):
    """Calcula segmentos y (fecha_ini, hora_ini, fecha_fin, hora_fin) del plan."""
    try:
        d0 = datetime.strptime((fecha_plan or today_mx())[:10], "%Y-%m-%d").date()
    except Exception:
        d0 = now_mx().date()
    try:
        h0 = min_of_hhmm(hora_inicio_plan or "08:00")
    except Exception:
        h0 = 8 * 60
    dur = float(minutos_plan or 0) or 30
    vf = parse_velada_fechas(velada_fechas)
    segs = empaquetar_en_jornada(d0, h0, dur, bool(sabado_autorizado), vf)
    if not segs:
        return {
            "segs": [],
            "fecha_ini": (fecha_plan or "")[:10],
            "hora_ini": (hora_inicio_plan or "08:00")[:5],
            "fecha_fin": (fecha_plan or "")[:10],
            "hora_fin": (hora_inicio_plan or "08:00")[:5],
            "dias": 0,
            "min_total": dur,
        }
    first, last = segs[0], segs[-1]
    return {
        "segs": segs,
        "fecha_ini": first[0].strftime("%Y-%m-%d"),
        "hora_ini": min_to_hhmm(first[1]),
        "fecha_fin": last[0].strftime("%Y-%m-%d"),
        "hora_fin": min_to_hhmm(last[2]),
        "dias": (last[0] - first[0]).days + 1,
        "min_total": sum(s[2] - s[1] for s in segs),
    }


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
        fi = (request.form.get("fecha_inicio") or "").strip() or today_mx()
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
            now = now_str_mx()
            if USE_POSTGRES:
                velada_auth = 1 if (request.form.get("velada_autorizada") and is_admin_or_gerente()) else 0
                cur.execute(
                    "INSERT INTO registros (trabajo_id,asignacion_id,bordado_id,operador,maquina_id,piezas,"
                    "puntadas_unit,minutos_proyectados,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
                    "incidencia,observaciones,velada_autorizada,registrado_por,creado_en) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (tid, aid, bid, operador, maquina_id, piezas, puntadas_unit, mins_proy,
                     fi, hi, ff or None, hf or None, incidencia, obs, velada_auth, session.get("username"), now))
                rid = as_dict(cur.fetchone())["id"]
                cur.execute("UPDATE trabajos SET estado='En proceso' WHERE id=%s AND estado IN ('Recibido','Asignado')", (tid,))
                if aid:
                    cur.execute("UPDATE asignaciones SET estado='En proceso' WHERE id=%s AND estado='Pendiente'", (aid,))
            else:
                velada_auth = 1 if (request.form.get("velada_autorizada") and is_admin_or_gerente()) else 0
                cur.execute(
                    "INSERT INTO registros (trabajo_id,asignacion_id,bordado_id,operador,maquina_id,piezas,"
                    "puntadas_unit,minutos_proyectados,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
                    "incidencia,observaciones,velada_autorizada,registrado_por,creado_en) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, aid, bid, operador, maquina_id, piezas, puntadas_unit, mins_proy,
                     fi, hi, ff or None, hf or None, incidencia, obs, velada_auth, session.get("username"), now))
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
    hoy = today_mx()
    ahora = now_time_mx()
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
        + ('<label style="margin-top:.5rem"><input type="checkbox" name="velada_autorizada" value="1"> Autorizar velada (18:00-02:00) — solo cuenta si se marca</label>' if is_admin_or_gerente() else '<p style="font-size:.78rem;color:#64748b">La velada solo cuenta si gerente/admin la autoriza.</p>')
        + '<p style="font-size:.78rem;color:#64748b">Puedes dejar fin vacio y completarlo despues. Sabado 8:00-17:30.</p>'
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
        velada_auth = None
        if is_admin_or_gerente():
            velada_auth = 1 if request.form.get("velada_autorizada") else 0
        if USE_POSTGRES:
            if velada_auth is None:
                cur.execute(
                    "UPDATE registros SET fecha_fin=%s,hora_fin=%s,piezas=%s,incidencia=%s,observaciones=%s WHERE id=%s",
                    (ff or None, hf or None, piezas, incidencia, obs, reg_id))
            else:
                cur.execute(
                    "UPDATE registros SET fecha_fin=%s,hora_fin=%s,piezas=%s,incidencia=%s,observaciones=%s,velada_autorizada=%s WHERE id=%s",
                    (ff or None, hf or None, piezas, incidencia, obs, velada_auth, reg_id))
        else:
            if velada_auth is None:
                cur.execute(
                    "UPDATE registros SET fecha_fin=?,hora_fin=?,piezas=?,incidencia=?,observaciones=? WHERE id=?",
                    (ff or None, hf or None, piezas, incidencia, obs, reg_id))
            else:
                cur.execute(
                    "UPDATE registros SET fecha_fin=?,hora_fin=?,piezas=?,incidencia=?,observaciones=?,velada_autorizada=? WHERE id=?",
                    (ff or None, hf or None, piezas, incidencia, obs, velada_auth, reg_id))
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
    mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"), velada_flag(r))
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
            + ('<label style="margin-top:.4rem"><input type="checkbox" name="velada_autorizada" value="1" %s> Autorizar velada (solo cuenta si se marca)</label>' % ("checked" if velada_flag(r) else "",)
               if is_admin_or_gerente() else "")
            + '<button class="btn btn-primary" style="margin-top:.5rem">Guardar</button></form>'
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
    hoy = today_mx()
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
            mt = minutos_trabajados(x.get("fecha_inicio"), x.get("hora_inicio"), x.get("fecha_fin"), x.get("hora_fin"), velada_flag(x))
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


@app.route("/ponchados")
@login_required
def ponchados_lista():
    q = (request.args.get("q") or "").strip()
    conn = get_db(); cur = dcur(conn)
    if q:
        like = "%" + q + "%"
        if USE_POSTGRES:
            cur.execute(
                "SELECT * FROM ponchados WHERE logo ILIKE %s OR cliente ILIKE %s OR usuario ILIKE %s "
                "ORDER BY id DESC LIMIT 200", (like, like, like))
        else:
            cur.execute(
                "SELECT * FROM ponchados WHERE logo LIKE ? OR cliente LIKE ? OR usuario LIKE ? "
                "ORDER BY id DESC LIMIT 200", (like, like, like))
    else:
        cur.execute("SELECT * FROM ponchados ORDER BY id DESC LIMIT 200")
    rows = as_dicts(cur.fetchall()); conn.close()
    tr = ""
    for p in rows:
        mt = minutos_trabajados(p.get("fecha_inicio"), p.get("hora_inicio"), p.get("fecha_fin"), p.get("hora_fin"), velada_flag(p))
        estado = '<span class="badge badge-warn">En proceso</span>' if mt["abierto"] else '<span class="badge badge-ok">Cerrado</span>'
        tr += (
            '<tr><td><a href="/ponchado/%s"><strong>%s</strong></a></td><td>%s</td><td>%s</td>'
            "<td>%s %s</td><td>%s %s</td><td>%s</td><td>%s min</td><td>%s</td>"
            '<td><a class="btn btn-outline btn-sm" href="/ponchado/%s">Ver</a></td></tr>'
        ) % (
            p["id"], p.get("logo") or "-",
            p.get("cliente") or "-",
            "{:,}".format(int(p.get("puntadas") or 0)),
            p.get("fecha_inicio") or "", p.get("hora_inicio") or "",
            p.get("fecha_fin") or "-", p.get("hora_fin") or "",
            p.get("usuario") or "-",
            mt["total"],
            estado,
            p["id"],
        )
    body = (
        '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem;align-items:center">'
        '<h1 style="margin:0">Ponchados (digitalizacion)</h1>'
        '<a class="btn btn-primary" href="/ponchado/nuevo">+ Nuevo ponchado</a></div>'
        '<p style="color:#64748b;font-size:.9rem;margin:.4rem 0">Registro de digitalizacion de logos. Suma a la productividad del usuario.</p>'
        '<form method="GET" style="margin:.5rem 0;display:flex;gap:.4rem">'
        '<input name="q" value="%s" placeholder="Buscar logo, cliente, usuario" style="max-width:280px">'
        '<button class="btn btn-outline">Buscar</button></form>'
        '<table style="font-size:.85rem"><thead><tr>'
        '<th>Logo</th><th>Cliente</th><th>Puntadas</th><th>Inicio</th><th>Fin</th><th>Usuario</th><th>Min</th><th></th><th></th>'
        '</tr></thead><tbody>%s</tbody></table></div>'
    ) % (q, tr or "<tr><td colspan=9>Sin ponchados</td></tr>")
    return page("Ponchados", body, session)

@app.route("/ponchado/nuevo", methods=["GET", "POST"])
@login_required
@edit_required
def ponchado_nuevo():
    err = ""
    if request.method == "POST":
        logo = (request.form.get("logo") or "").strip()
        cliente = (request.form.get("cliente") or "").strip()
        try:
            puntadas = int(request.form.get("puntadas") or 0)
        except Exception:
            puntadas = 0
        fi = (request.form.get("fecha_inicio") or "").strip() or today_mx()
        hi = (request.form.get("hora_inicio") or "").strip()
        ff = (request.form.get("fecha_fin") or "").strip()
        hf = (request.form.get("hora_fin") or "").strip()
        usuario = (request.form.get("usuario") or "").strip() or (session.get("nombre") or session.get("username") or "")
        obs = (request.form.get("observaciones") or "").strip()
        if not logo:
            err = "Indica el nombre del logo"
        elif not hi:
            err = "Indica hora de inicio"
        else:
            now = now_str_mx()
            conn = get_db(); cur = dcur(conn)
            velada_auth = 1 if (request.form.get("velada_autorizada") and is_admin_or_gerente()) else 0
            if USE_POSTGRES:
                cur.execute(
                    "INSERT INTO ponchados (logo,cliente,puntadas,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
                    "usuario,observaciones,velada_autorizada,creado_en) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (logo, cliente, puntadas, fi, hi, ff or None, hf or None, usuario, obs, velada_auth, now))
                pid = as_dict(cur.fetchone())["id"]
            else:
                cur.execute(
                    "INSERT INTO ponchados (logo,cliente,puntadas,fecha_inicio,hora_inicio,fecha_fin,hora_fin,"
                    "usuario,observaciones,velada_autorizada,creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (logo, cliente, puntadas, fi, hi, ff or None, hf or None, usuario, obs, velada_auth, now))
                pid = cur.lastrowid
            conn.commit(); conn.close()
            return redirect(url_for("ponchado_ver", ponchado_id=pid))
    hoy = today_mx()
    ahora = now_time_mx()
    user_def = session.get("nombre") or session.get("username") or ""
    body = (
        '<div class="card"><h1>Nuevo ponchado</h1>'
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST"><div class="form-row">'
        '<div><label>Nombre del logo</label><input name="logo" required placeholder="Nombre del logo / diseno"></div>'
        '<div><label>Cliente</label><input name="cliente"></div>'
        '<div><label>Puntadas</label><input type="number" name="puntadas" min="0" value="0"></div></div>'
        '<div class="form-row">'
        '<div><label>Fecha inicio</label><input type="date" name="fecha_inicio" value="%s" required></div>'
        '<div><label>Hora inicio</label><input type="time" name="hora_inicio" value="%s" required></div>'
        '<div><label>Fecha fin</label><input type="date" name="fecha_fin"></div>'
        '<div><label>Hora fin</label><input type="time" name="hora_fin"></div></div>'
        '<div class="form-row">'
        '<div><label>Usuario (quien digitaliza)</label><input name="usuario" value="%s" required></div></div>'
        '<label>Observaciones</label><textarea name="observaciones" rows="2"></textarea>'
        + ('<label style="margin-top:.5rem"><input type="checkbox" name="velada_autorizada" value="1"> Autorizar velada</label>' if is_admin_or_gerente() else '<p style="font-size:.78rem;color:#64748b">Velada solo si gerente/admin la autoriza.</p>')
        + '<p style="font-size:.78rem;color:#64748b">Puedes dejar fin vacio y completarlo despues.</p>'
        '<button class="btn btn-primary">Guardar</button> '
        '<a class="btn btn-outline" href="/ponchados">Cancelar</a></form></div>'
    ) % (hoy, ahora, user_def)
    return page("Nuevo ponchado", body, session)

@app.route("/ponchado/<int:ponchado_id>", methods=["GET", "POST"])
@login_required
def ponchado_ver(ponchado_id):
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT * FROM ponchados WHERE id=" + ph(), (ponchado_id,))
    p = as_dict(cur.fetchone())
    if not p:
        conn.close()
        return redirect(url_for("ponchados_lista"))
    if request.method == "POST" and can_edit():
        logo = (request.form.get("logo") or p.get("logo") or "").strip()
        cliente = (request.form.get("cliente") or "").strip()
        try:
            puntadas = int(request.form.get("puntadas") or p.get("puntadas") or 0)
        except Exception:
            puntadas = int(p.get("puntadas") or 0)
        fi = (request.form.get("fecha_inicio") or p.get("fecha_inicio") or "").strip()
        hi = (request.form.get("hora_inicio") or p.get("hora_inicio") or "").strip()
        ff = (request.form.get("fecha_fin") or "").strip()
        hf = (request.form.get("hora_fin") or "").strip()
        usuario = (request.form.get("usuario") or p.get("usuario") or "").strip()
        obs = (request.form.get("observaciones") or "").strip()
        velada_auth = None
        if is_admin_or_gerente():
            velada_auth = 1 if request.form.get("velada_autorizada") else 0
        if USE_POSTGRES:
            if velada_auth is None:
                cur.execute(
                    "UPDATE ponchados SET logo=%s,cliente=%s,puntadas=%s,fecha_inicio=%s,hora_inicio=%s,"
                    "fecha_fin=%s,hora_fin=%s,usuario=%s,observaciones=%s WHERE id=%s",
                    (logo, cliente, puntadas, fi, hi, ff or None, hf or None, usuario, obs, ponchado_id))
            else:
                cur.execute(
                    "UPDATE ponchados SET logo=%s,cliente=%s,puntadas=%s,fecha_inicio=%s,hora_inicio=%s,"
                    "fecha_fin=%s,hora_fin=%s,usuario=%s,observaciones=%s,velada_autorizada=%s WHERE id=%s",
                    (logo, cliente, puntadas, fi, hi, ff or None, hf or None, usuario, obs, velada_auth, ponchado_id))
        else:
            if velada_auth is None:
                cur.execute(
                    "UPDATE ponchados SET logo=?,cliente=?,puntadas=?,fecha_inicio=?,hora_inicio=?,"
                    "fecha_fin=?,hora_fin=?,usuario=?,observaciones=? WHERE id=?",
                    (logo, cliente, puntadas, fi, hi, ff or None, hf or None, usuario, obs, ponchado_id))
            else:
                cur.execute(
                    "UPDATE ponchados SET logo=?,cliente=?,puntadas=?,fecha_inicio=?,hora_inicio=?,"
                    "fecha_fin=?,hora_fin=?,usuario=?,observaciones=?,velada_autorizada=? WHERE id=?",
                    (logo, cliente, puntadas, fi, hi, ff or None, hf or None, usuario, obs, velada_auth, ponchado_id))
        conn.commit()
        cur.execute("SELECT * FROM ponchados WHERE id=" + ph(), (ponchado_id,))
        p = as_dict(cur.fetchone())
    conn.close()
    mt = minutos_trabajados(p.get("fecha_inicio"), p.get("hora_inicio"), p.get("fecha_fin"), p.get("hora_fin"), velada_flag(p))
    form = ""
    if can_edit():
        form = (
            '<form method="POST" class="card"><h2>Actualizar / cerrar</h2><div class="form-row">'
            '<div><label>Logo</label><input name="logo" value="%s" required></div>'
            '<div><label>Cliente</label><input name="cliente" value="%s"></div>'
            '<div><label>Puntadas</label><input type="number" name="puntadas" value="%s" min="0"></div>'
            '<div><label>Usuario</label><input name="usuario" value="%s"></div></div>'
            '<div class="form-row">'
            '<div><label>Fecha inicio</label><input type="date" name="fecha_inicio" value="%s"></div>'
            '<div><label>Hora inicio</label><input type="time" name="hora_inicio" value="%s"></div>'
            '<div><label>Fecha fin</label><input type="date" name="fecha_fin" value="%s"></div>'
            '<div><label>Hora fin</label><input type="time" name="hora_fin" value="%s"></div></div>'
            '<label>Observaciones</label><textarea name="observaciones" rows="2">%s</textarea>'
            + ('<label style="margin-top:.4rem"><input type="checkbox" name="velada_autorizada" value="1" %s> Autorizar velada</label>' % ("checked" if velada_flag(p) else "",)
               if is_admin_or_gerente() else "")
            + '<button class="btn btn-primary" style="margin-top:.5rem">Guardar</button></form>'
        ) % (
            p.get("logo") or "", p.get("cliente") or "", int(p.get("puntadas") or 0),
            p.get("usuario") or "",
            p.get("fecha_inicio") or "", (p.get("hora_inicio") or "")[:5],
            p.get("fecha_fin") or "", (p.get("hora_fin") or "")[:5],
            p.get("observaciones") or "",
        )
    body = (
        '<div class="card"><h1>Ponchado: %s</h1>'
        '<div class="grid3">'
        '<div class="stat"><div class="n" style="font-size:1.1rem">%s</div><div class="l">Cliente</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Usuario</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min jornada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min velada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min totales</div></div></div>'
        '<p style="font-size:.85rem;margin-top:.7rem;color:#64748b">'
        "Inicio: <strong>%s %s</strong> | Fin: <strong>%s %s</strong>%s</p></div>"
        '%s<a class="btn btn-outline" href="/ponchados">Volver</a>'
    ) % (
        p.get("logo") or "-",
        p.get("cliente") or "-",
        "{:,}".format(int(p.get("puntadas") or 0)),
        p.get("usuario") or "-",
        mt["jornada"], mt["velada"], mt["total"],
        p.get("fecha_inicio") or "", p.get("hora_inicio") or "",
        p.get("fecha_fin") or "-", p.get("hora_fin") or "",
        ' <span class="badge badge-warn">En proceso</span>' if mt["abierto"] else "",
        form,
    )
    return page("Ponchado", body, session)


@app.route("/operador")
@app.route("/operador/<path:nombre>")
@login_required
def operador_vista(nombre=None):
    """Vista de productividad por operador: dia a dia, minutos, ponchados, puntadas."""
    fecha_ref = (request.args.get("fecha") or today_mx()).strip()
    periodo = (request.args.get("periodo") or "semana").strip()
    try:
        ref = datetime.strptime(fecha_ref[:10], "%Y-%m-%d").date()
    except Exception:
        ref = now_mx().date()
        fecha_ref = ref.strftime("%Y-%m-%d")
    if periodo == "dia":
        fi = ff = ref
        titulo_p = "Dia " + ref.strftime("%d/%m/%Y")
    elif periodo == "mes":
        fi = ref.replace(day=1)
        if ref.month == 12:
            ff = ref.replace(year=ref.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            ff = ref.replace(month=ref.month + 1, day=1) - timedelta(days=1)
        titulo_p = "Mes " + ref.strftime("%m/%Y")
    else:
        periodo = "semana"
        fi = ref - timedelta(days=ref.weekday())
        ff = fi + timedelta(days=6)
        titulo_p = "Semana %s - %s" % (fi.strftime("%d/%m"), ff.strftime("%d/%m/%Y"))
    fi_s, ff_s = fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d")

    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT DISTINCT operador FROM registros WHERE operador IS NOT NULL AND operador!='' ORDER BY operador")
    ops_r = [as_dict(x).get("operador") for x in cur.fetchall()]
    cur.execute("SELECT DISTINCT usuario FROM ponchados WHERE usuario IS NOT NULL AND usuario!='' ORDER BY usuario")
    ops_p = [as_dict(x).get("usuario") for x in cur.fetchall()]
    ops = sorted(set([o for o in ops_r + ops_p if o]))
    if not nombre:
        if session.get("rol") == "operador":
            nombre = session.get("nombre") or session.get("username")
        elif ops:
            nombre = ops[0]
        else:
            nombre = session.get("nombre") or "Operador"
    # filter data
    if USE_POSTGRES:
        cur.execute(
            "SELECT * FROM registros WHERE fecha_inicio>=%s AND fecha_inicio<=%s AND operador ILIKE %s ORDER BY fecha_inicio, id",
            (fi_s, ff_s, nombre))
        regs = as_dicts(cur.fetchall())
        cur.execute(
            "SELECT * FROM ponchados WHERE fecha_inicio>=%s AND fecha_inicio<=%s AND usuario ILIKE %s ORDER BY fecha_inicio, id",
            (fi_s, ff_s, nombre))
        ponchs = as_dicts(cur.fetchall())
    else:
        cur.execute(
            "SELECT * FROM registros WHERE fecha_inicio>=? AND fecha_inicio<=? AND operador LIKE ? ORDER BY fecha_inicio, id",
            (fi_s, ff_s, nombre))
        regs = as_dicts(cur.fetchall())
        cur.execute(
            "SELECT * FROM ponchados WHERE fecha_inicio>=? AND fecha_inicio<=? AND usuario LIKE ? ORDER BY fecha_inicio, id",
            (fi_s, ff_s, nombre))
        ponchs = as_dicts(cur.fetchall())
    conn.close()

    by_day = {}
    tot = {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "ponch": 0, "punt_ponch": 0, "regs": 0}
    for r in regs:
        d = (r.get("fecha_inicio") or "")[:10]
        st = by_day.setdefault(d, {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "ponch": 0, "punt_ponch": 0, "regs": 0})
        pzas = int(r.get("piezas") or 0)
        pu = int(r.get("puntadas_unit") or 0)
        mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"), velada_flag(r))
        st["pzas"] += pzas; st["punt"] += pzas * pu
        st["jornada"] += mt["jornada"]; st["velada"] += mt["velada"]; st["total"] += mt["total"]
        st["regs"] += 1
        tot["pzas"] += pzas; tot["punt"] += pzas * pu
        tot["jornada"] += mt["jornada"]; tot["velada"] += mt["velada"]; tot["total"] += mt["total"]
        tot["regs"] += 1
    for p in ponchs:
        d = (p.get("fecha_inicio") or "")[:10]
        st = by_day.setdefault(d, {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "ponch": 0, "punt_ponch": 0, "regs": 0})
        punt = int(p.get("puntadas") or 0)
        mt = minutos_trabajados(p.get("fecha_inicio"), p.get("hora_inicio"), p.get("fecha_fin"), p.get("hora_fin"), velada_flag(p))
        st["ponch"] += 1; st["punt_ponch"] += punt; st["punt"] += punt
        st["jornada"] += mt["jornada"]; st["velada"] += mt["velada"]; st["total"] += mt["total"]
        tot["ponch"] += 1; tot["punt_ponch"] += punt; tot["punt"] += punt
        tot["jornada"] += mt["jornada"]; tot["velada"] += mt["velada"]; tot["total"] += mt["total"]

    drows = ""
    for d in sorted(by_day.keys(), reverse=True):
        st = by_day[d]
        drows += (
            "<tr><td><strong>%s</strong></td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        ) % (
            d, st["regs"], st["pzas"], "{:,}".format(st["punt"]),
            st["ponch"], "{:,}".format(st["punt_ponch"]),
            st["jornada"], st["velada"], st["total"],
        )

    # selector operadores
    sel = ""
    for o in ops:
        sel += '<option value="%s"%s>%s</option>' % (o, " selected" if o == nombre else "", o)
    tabs = ""
    for key, lab in [("dia", "Dia"), ("semana", "Semana"), ("mes", "Mes")]:
        cls = "btn-primary" if periodo == key else "btn-outline"
        tabs += '<a class="btn %s btn-sm" href="/operador/%s?periodo=%s&fecha=%s">%s</a> ' % (
            cls, nombre, key, fecha_ref, lab)

    body = (
        '<div class="card"><h1>Productividad por operador</h1>'
        '<form method="GET" action="/operador" style="display:flex;flex-wrap:wrap;gap:.4rem;align-items:end;margin:.5rem 0">'
        '<div><label>Operador</label><select name="go" onchange="location=\'/operador/\'+encodeURIComponent(this.value)+\'?periodo=%s&fecha=%s\'">%s</select></div>'
        '<div><label>Fecha ref.</label><input type="date" name="fecha" value="%s" '
        'onchange="location=\'/operador/%s?periodo=%s&fecha=\'+this.value"></div></form>'
        '<div style="margin:.4rem 0">%s</div>'
        '<p style="color:#64748b">%s — <strong>%s</strong></p>'
        '<div class="grid3">'
        '<div class="stat"><div class="n">%s</div><div class="l">Piezas bordado</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas total</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Ponchados</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas ponchado</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min jornada</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min velada (autorizada)</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min totales</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Registros bordado</div></div></div></div>'
        '<div class="card"><h2>Detalle por dia</h2>'
        '<table style="font-size:.85rem"><thead><tr>'
        '<th>Fecha</th><th>Regs</th><th>Piezas</th><th>Puntadas</th><th>Ponchados</th><th>Punt. ponch</th>'
        '<th>Min jor</th><th>Min vel</th><th>Min tot</th></tr></thead>'
        '<tbody>%s</tbody></table>'
        '<p style="font-size:.78rem;color:#64748b;margin-top:.5rem">'
        "La velada solo se suma si un gerente/admin marco <strong>Autorizar velada</strong> en el registro o ponchado.</p></div>"
    ) % (
        periodo, fecha_ref, sel or "<option>Sin datos</option>",
        fecha_ref, nombre, periodo,
        tabs, titulo_p, nombre,
        tot["pzas"], "{:,}".format(tot["punt"]), tot["ponch"], "{:,}".format(tot["punt_ponch"]),
        tot["jornada"], tot["velada"], tot["total"], tot["regs"],
        drows or "<tr><td colspan=9>Sin actividad en el periodo</td></tr>",
    )
    return page("Operador " + (nombre or ""), body, session)

@app.route("/trabajo/<int:trabajo_id>/eliminar", methods=["POST"])
@login_required
def trabajo_eliminar(trabajo_id):
    """Solo admin puede eliminar un trabajo completo. No se borra solo por redeploy."""
    if session.get("rol") != "admin":
        return page("Sin permiso", '<div class="alert alert-err">Solo el usuario admin puede eliminar trabajos.</div>', session)
    conn = get_db(); cur = dcur(conn)
    # borrar dependencias
    for table in ("registros", "asignaciones", "bordados", "historial"):
        try:
            if USE_POSTGRES:
                cur.execute("DELETE FROM " + table + " WHERE trabajo_id=%s", (trabajo_id,))
            else:
                cur.execute("DELETE FROM " + table + " WHERE trabajo_id=?", (trabajo_id,))
        except Exception as e:
            print("del", table, e)
    if USE_POSTGRES:
        cur.execute("DELETE FROM trabajos WHERE id=%s", (trabajo_id,))
    else:
        cur.execute("DELETE FROM trabajos WHERE id=?", (trabajo_id,))
    conn.commit(); conn.close()
    return redirect(url_for("trabajos"))





@app.route("/asignacion/<int:asig_id>/dividir", methods=["GET", "POST"])
@login_required
def asignacion_dividir(asig_id):
    """Divide una asignacion grande entre varios operadores/maquinas para acortar el calendario."""
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin</div>', session)
    conn = get_db(); cur = dcur(conn)
    cur.execute("SELECT a.*, t.vale, t.proyecto FROM asignaciones a JOIN trabajos t ON t.id=a.trabajo_id WHERE a.id=" + ph(), (asig_id,))
    a = as_dict(cur.fetchone())
    if not a:
        conn.close()
        return redirect(url_for("trabajos"))
    tid = a.get("trabajo_id")
    err = ""
    if request.method == "POST":
        # parts: operador[], maquina_id[], piezas[], minutos[]
        ops = request.form.getlist("operador")
        maqs = request.form.getlist("maquina_id")
        pzas_l = request.form.getlist("piezas")
        mins_l = request.form.getlist("minutos")
        sab = 1 if a.get("sabado_autorizado") else 0
        vf = a.get("velada_fechas") or ""
        partes = []
        for i, op in enumerate(ops):
            op = (op or "").strip()
            if not op:
                continue
            mq = (maqs[i] if i < len(maqs) else "") or ""
            try:
                pz = int(pzas_l[i] if i < len(pzas_l) else 0)
            except Exception:
                pz = 0
            try:
                mi = float(str(mins_l[i] if i < len(mins_l) else 0).replace(",", "."))
            except Exception:
                mi = 0
            if mi <= 0 and pz > 0:
                # proportional from original
                orig_p = int(a.get("piezas_objetivo") or 0) or 1
                orig_m = float(a.get("minutos_plan") or 0)
                mi = orig_m * (pz / orig_p)
            if mi > 0 or pz > 0:
                partes.append((op, mq, pz, mi))
        if len(partes) < 2:
            err = "Indica al menos 2 partes (operadores) para dividir"
        else:
            # cancel/replace original: mark terminado with obs or delete
            if USE_POSTGRES:
                cur.execute("DELETE FROM asignaciones WHERE id=%s", (asig_id,))
            else:
                cur.execute("DELETE FROM asignaciones WHERE id=?", (asig_id,))
            now = now_str_mx()
            for op, mq, pz, mi in partes:
                # each starts from now queue of that operator
                if USE_POSTGRES:
                    cur.execute(
                        "SELECT * FROM asignaciones WHERE operador=%s AND estado IN ('Pendiente','En proceso') "
                        "ORDER BY fecha_plan, hora_inicio_plan, id", (op,))
                else:
                    cur.execute(
                        "SELECT * FROM asignaciones WHERE operador=? AND estado IN ('Pendiente','En proceso') "
                        "ORDER BY fecha_plan, hora_inicio_plan, id", (op,))
                prev = as_dicts(cur.fetchall())
                d0 = now_mx().date()
                if not es_dia_laboral(d0, bool(sab)):
                    d0 = siguiente_dia_laboral(d0, bool(sab))
                h0 = 8 * 60
                for p in prev:
                    segs_p = empaquetar_en_jornada(
                        datetime.strptime((p.get("fecha_plan") or today_mx())[:10], "%Y-%m-%d").date(),
                        min_of_hhmm(p.get("hora_inicio_plan") or "08:00"),
                        float(p.get("minutos_plan") or 0) or 30,
                        bool(p.get("sabado_autorizado")), parse_velada_fechas(p.get("velada_fechas")))
                    if segs_p:
                        last = segs_p[-1]
                        if last[0] > d0 or (last[0] == d0 and last[2] > h0):
                            d0, h0 = last[0], last[2]
                segs_n = empaquetar_en_jornada(d0, h0, mi or 30, bool(sab), parse_velada_fechas(vf))
                if segs_n:
                    fp, hi = segs_n[0][0].strftime("%Y-%m-%d"), min_to_hhmm(segs_n[0][1])
                    ff, hf = segs_n[-1][0].strftime("%Y-%m-%d"), min_to_hhmm(segs_n[-1][2])
                else:
                    fp, hi, ff, hf = today_mx(), "08:00", today_mx(), "08:00"
                if USE_POSTGRES:
                    cur.execute(
                        "INSERT INTO asignaciones (trabajo_id,bordado_id,operador,maquina_id,piezas_objetivo,minutos_plan,"
                        "fecha_plan,hora_inicio_plan,fecha_fin_plan,hora_fin_plan,sabado_autorizado,velada_fechas,"
                        "estado,fecha_asignacion,asignado_por,observaciones) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pendiente',%s,%s,%s)",
                        (tid, a.get("bordado_id"), op, mq, pz, mi, fp, hi, ff, hf, sab, vf,
                         now, session.get("username"), "Dividido desde asig #%s" % asig_id))
                else:
                    cur.execute(
                        "INSERT INTO asignaciones (trabajo_id,bordado_id,operador,maquina_id,piezas_objetivo,minutos_plan,"
                        "fecha_plan,hora_inicio_plan,fecha_fin_plan,hora_fin_plan,sabado_autorizado,velada_fechas,"
                        "estado,fecha_asignacion,asignado_por,observaciones) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'Pendiente',?,?,?)",
                        (tid, a.get("bordado_id"), op, mq, pz, mi, fp, hi, ff, hf, sab, vf,
                         now, session.get("username"), "Dividido desde asig #%s" % asig_id))
            log_evento(cur, tid, "Division", "Asig #%s dividida en %s partes" % (asig_id, len(partes)))
            # repack each operator
            for op, _, _, _ in partes:
                repack_operador(cur, op)
            conn.commit(); conn.close()
            return redirect(url_for("trabajo_ver", trabajo_id=tid))

    conn.close()
    opts_m = "".join('<option value="%s">%s</option>' % (m["id"], m["nombre"]) for m in MAQUINAS)
    slots = ""
    for n in range(1, 5):
        slots += (
            '<div class="bordado-card"><strong>Parte %s</strong><div class="form-row">'
            '<div><label>Operador</label><input name="operador" placeholder="Nombre"></div>'
            '<div><label>Maquina</label><select name="maquina_id"><option value="">-</option>%s</select></div>'
            '<div><label>Piezas</label><input type="number" name="piezas" min="0" value="0"></div>'
            '<div><label>Minutos plan</label><input type="number" name="minutos" min="0" step="1" '
            'placeholder="Auto si vacio y hay piezas"></div></div></div>'
        ) % (n, opts_m)
    tit = a.get("vale") or a.get("proyecto") or ("#" + str(tid))
    body = (
        '<div class="card"><h1>Dividir asignacion</h1>'
        '<p>Trabajo <strong>%s</strong> — actual: <strong>%s</strong> en %s | %.0f min | %s pzas</p>'
        '<p style="font-size:.85rem;color:#64748b">Al dividir, la asignacion original se reemplaza por varias en paralelo '
        "(cada operador en su maquina). El calendario se acorta si trabajan al mismo tiempo.</p>"
        + (('<div class="alert alert-err">' + err + "</div>") if err else "")
        + '<form method="POST">%s'
        '<button class="btn btn-primary">Dividir y reprogramar</button> '
        '<a class="btn btn-outline" href="/trabajo/%s">Cancelar</a></form></div>'
    ) % (
        tit, a.get("operador"), nombre_maq(a.get("maquina_id")) if a.get("maquina_id") else "-",
        float(a.get("minutos_plan") or 0), a.get("piezas_objetivo") or 0,
        slots, tid,
    )
    return page("Dividir", body, session)


@app.route("/cola/lista")
@login_required
def cola_lista():
    """Lista interactiva de la cola: subir/bajar, posponer, reasignar. Recalcula Gantt."""
    operador_f = (request.args.get("operador") or "").strip()
    fecha = (request.args.get("fecha") or today_mx()).strip()
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute(
            "SELECT a.*, t.vale, t.proyecto, t.estado as estado_trabajo FROM asignaciones a "
            "JOIN trabajos t ON t.id=a.trabajo_id "
            "WHERE a.estado IN ('Pendiente','En proceso') "
            "ORDER BY a.operador, a.fecha_plan, a.hora_inicio_plan, a.id")
    else:
        cur.execute(
            "SELECT a.*, t.vale, t.proyecto, t.estado as estado_trabajo FROM asignaciones a "
            "JOIN trabajos t ON t.id=a.trabajo_id "
            "WHERE a.estado IN ('Pendiente','En proceso') "
            "ORDER BY a.operador, a.fecha_plan, a.hora_inicio_plan, a.id")
    rows = as_dicts(cur.fetchall())
    ops = sorted(set((r.get("operador") or "").strip() for r in rows if r.get("operador")))
    # tiempos reales por asignacion_id
    real_map = {}
    aids = [r["id"] for r in rows]
    if aids:
        # registros ligados a asignacion
        if USE_POSTGRES:
            cur.execute(
                "SELECT * FROM registros WHERE asignacion_id IS NOT NULL ORDER BY id")
        else:
            cur.execute(
                "SELECT * FROM registros WHERE asignacion_id IS NOT NULL ORDER BY id")
        for reg in as_dicts(cur.fetchall()):
            aid = reg.get("asignacion_id")
            if not aid:
                continue
            mt = minutos_trabajados(reg.get("fecha_inicio"), reg.get("hora_inicio"),
                                    reg.get("fecha_fin"), reg.get("hora_fin"), velada_flag(reg))
            st = real_map.setdefault(aid, {"min": 0, "ini": "", "fin": ""})
            st["min"] += mt["total"]
            ini = "%s %s" % (reg.get("fecha_inicio") or "", (reg.get("hora_inicio") or "")[:5])
            fin = "%s %s" % (reg.get("fecha_fin") or "", (reg.get("hora_fin") or "")[:5])
            if not st["ini"] or ini < st["ini"]:
                st["ini"] = ini.strip()
            if fin.strip() and (not st["fin"] or fin > st["fin"]):
                st["fin"] = fin.strip()
    conn.close()
    if operador_f:
        rows = [r for r in rows if (r.get("operador") or "").strip() == operador_f]

    # group by operator
    groups = {}
    for r in rows:
        groups.setdefault((r.get("operador") or "-").strip(), []).append(r)

    blocks = ""
    for op in sorted(groups.keys()):
        items = groups[op]
        tr = ""
        for i, a in enumerate(items):
            tit = a.get("vale") or a.get("proyecto") or ("#" + str(a.get("trabajo_id")))
            can_edit = is_admin_or_gerente()
            btns = ""
            if can_edit:
                btns = (
                    '<form method="POST" action="/cola/mover" style="display:inline">'
                    '<input type="hidden" name="id" value="%s">'
                    '<input type="hidden" name="dir" value="up">'
                    '<input type="hidden" name="operador" value="%s">'
                    '<button class="btn btn-outline btn-sm" title="Subir">▲</button></form> '
                    '<form method="POST" action="/cola/mover" style="display:inline">'
                    '<input type="hidden" name="id" value="%s">'
                    '<input type="hidden" name="dir" value="down">'
                    '<input type="hidden" name="operador" value="%s">'
                    '<button class="btn btn-outline btn-sm" title="Bajar">▼</button></form> '
                    '<form method="POST" action="/cola/mover" style="display:inline">'
                    '<input type="hidden" name="id" value="%s">'
                    '<input type="hidden" name="dir" value="top">'
                    '<input type="hidden" name="operador" value="%s">'
                    '<button class="btn btn-outline btn-sm" title="Urgente al frente">⇈</button></form> '
                ) % (a["id"], op, a["id"], op, a["id"], op)
                btns += (
                    '<form method="POST" action="/cola/posponer" style="display:inline;margin-left:.3rem">'
                    '<input type="hidden" name="id" value="%s">'
                    '<input type="hidden" name="operador" value="%s">'
                    '<input type="date" name="fecha" value="%s" style="width:auto;padding:.2rem">'
                    '<button class="btn btn-warning btn-sm">Posponer</button></form> '
                    '<form method="POST" action="/cola/reasignar" style="display:inline">'
                    '<input type="hidden" name="id" value="%s">'
                    '<input type="hidden" name="operador_old" value="%s">'
                    '<input name="operador_new" placeholder="Otro operador" style="width:110px;padding:.2rem" required>'
                    '<button class="btn btn-outline btn-sm">Reasignar</button></form>'
                ) % (a["id"], op, a.get("fecha_plan") or today_mx(), a["id"], op)
            pv = plan_ventana(a.get("fecha_plan"), a.get("hora_inicio_plan"),
                              a.get("minutos_plan"), a.get("sabado_autorizado"), a.get("velada_fechas"))
            fi = a.get("fecha_fin_plan") or pv["fecha_fin"]
            hi_f = (a.get("hora_fin_plan") or pv["hora_fin"] or "")[:5]
            real_min = real_map.get(a["id"], {}).get("min", 0)
            real_ini = real_map.get(a["id"], {}).get("ini", "")
            real_fin = real_map.get(a["id"], {}).get("fin", "")
            delta = (real_min - float(a.get("minutos_plan") or 0)) if real_min else None
            delta_txt = (("+" if delta > 0 else "") + ("%.0f" % delta)) if delta is not None else "—"
            span = ""
            if pv["fecha_ini"] != pv["fecha_fin"]:
                span = '<br><small style="color:#b45309">Cruza %s dia(s) laborales</small>' % pv["dias"]
            tr += (
                "<tr><td>%s</td><td><a href=\"/trabajo/%s\"><strong>%s</strong></a>%s</td>"
                "<td>%s</td><td>%s</td><td>%.0f min</td>"
                "<td><strong>%s %s</strong><br>→ %s %s</td>"
                "<td>%s<br><small>Real: %s → %s (%s min)</small><br>"
                "<small>Dif plan: %s min</small></td>"
                "<td style=\"white-space:nowrap\">%s</td></tr>"
            ) % (
                i + 1, a.get("trabajo_id"), tit, span,
                nombre_maq(a.get("maquina_id")) if a.get("maquina_id") else "-",
                a.get("piezas_objetivo") or 0,
                float(a.get("minutos_plan") or 0),
                pv["fecha_ini"], pv["hora_ini"], fi, hi_f,
                badge_estado(a.get("estado")),
                real_ini or "—", real_fin or "—", real_min or 0,
                delta_txt,
                btns,
            )
        blocks += (
            '<div class="card"><h2>Operador: %s</h2>'
            '<table style="font-size:.85rem"><thead><tr>'
            "<th>#</th><th>Trabajo</th><th>Maquina</th><th>Pzas</th><th>Min plan</th>"
            "<th>Plan inicio → fin</th><th>Estado / Real</th><th>Acciones</th></tr></thead>"
            "<tbody>%s</tbody></table>"
            "%s</div>"
        ) % (
            op, tr or "<tr><td colspan=8>Sin pendientes</td></tr>",
            ('<form method="POST" action="/cola/repack" style="margin-top:.5rem">'
             '<input type="hidden" name="operador" value="%s">'
             '<button class="btn btn-primary btn-sm">Recalcular horario de esta cola</button></form>' % op)
            if is_admin_or_gerente() else "",
        )

    op_opts = '<option value="">Todos</option>' + "".join(
        '<option value="%s"%s>%s</option>' % (o, " selected" if o == operador_f else "", o) for o in ops)
    body = (
        '<div class="card"><h1>Cola interactiva</h1>'
        '<p style="color:#64748b;font-size:.9rem">Urgencias: sube (⇈), baja, pospone o reasigna. '
        "Si sumas mas minutos de los del dia, el plan pasa al siguiente dia laboral "
        "(viernes → lunes; no usa sabado/domingo salvo autorizacion). "
        "Cada fila muestra <strong>inicio y fin plan</strong> y lo real reportado.</p>"
        '<div style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0">'
        '<form method="GET" style="display:flex;gap:.35rem;align-items:center">'
        '<label>Operador</label><select name="operador" onchange="this.form.submit()">%s</select></form>'
        '<a class="btn btn-outline btn-sm" href="/cola">Ver Gantt</a>'
        '<a class="btn btn-outline btn-sm" href="/cola?vista=semana">Gantt semana</a></div></div>'
        "%s"
    ) % (op_opts, blocks or '<div class="card"><p>No hay asignaciones pendientes.</p></div>')
    return page("Cola lista", body, session)

@app.route("/cola/mover", methods=["POST"])
@login_required
def cola_mover():
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin</div>', session)
    try:
        aid = int(request.form.get("id") or 0)
    except Exception:
        aid = 0
    direction = (request.form.get("dir") or "").strip()
    operador = (request.form.get("operador") or "").strip()
    if not aid or not operador:
        return redirect(url_for("cola_lista", operador=operador))
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=%s AND estado IN ('Pendiente','En proceso') "
            "ORDER BY fecha_plan, hora_inicio_plan, id", (operador,))
    else:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=? AND estado IN ('Pendiente','En proceso') "
            "ORDER BY fecha_plan, hora_inicio_plan, id", (operador,))
    rows = as_dicts(cur.fetchall())
    ids = [r["id"] for r in rows]
    if aid not in ids:
        conn.close()
        return redirect(url_for("cola_lista", operador=operador))
    i = ids.index(aid)
    if direction == "up" and i > 0:
        ids[i], ids[i - 1] = ids[i - 1], ids[i]
    elif direction == "down" and i < len(ids) - 1:
        ids[i], ids[i + 1] = ids[i + 1], ids[i]
    elif direction == "top":
        ids.remove(aid)
        ids.insert(0, aid)
    elif direction == "bottom":
        ids.remove(aid)
        ids.append(aid)
    # aplicar orden temporal artificial y repack
    base = now_mx().date()
    if not es_dia_laboral(base, False):
        base = siguiente_dia_laboral(base, False)
    for n, xid in enumerate(ids):
        # fechas/horas provisionales en orden
        fp = base.strftime("%Y-%m-%d")
        hi = "08:00"
        if USE_POSTGRES:
            cur.execute("UPDATE asignaciones SET fecha_plan=%s, hora_inicio_plan=%s WHERE id=%s", (fp, hi, xid))
        else:
            cur.execute("UPDATE asignaciones SET fecha_plan=?, hora_inicio_plan=? WHERE id=?", (fp, hi, xid))
        # bump id order by setting hora to sequential minutes then repack
    # force order by updating with sequential sort keys via fecha+hora then repack
    # Better: update in order with temporary sequential timestamps
    for n, xid in enumerate(ids):
        hi = min_to_hhmm(8 * 60 + n)  # 08:00, 08:01, 08:02... just for sort
        if USE_POSTGRES:
            cur.execute("UPDATE asignaciones SET fecha_plan=%s, hora_inicio_plan=%s WHERE id=%s",
                        (base.strftime("%Y-%m-%d"), hi, xid))
        else:
            cur.execute("UPDATE asignaciones SET fecha_plan=?, hora_inicio_plan=? WHERE id=?",
                        (base.strftime("%Y-%m-%d"), hi, xid))
    repack_operador(cur, operador, base.strftime("%Y-%m-%d"))
    conn.commit(); conn.close()
    return redirect(url_for("cola_lista", operador=operador))

@app.route("/cola/posponer", methods=["POST"])
@login_required
def cola_posponer():
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin</div>', session)
    try:
        aid = int(request.form.get("id") or 0)
    except Exception:
        aid = 0
    operador = (request.form.get("operador") or "").strip()
    fecha = (request.form.get("fecha") or "").strip() or today_mx()
    try:
        d = datetime.strptime(fecha[:10], "%Y-%m-%d").date()
    except Exception:
        d = now_mx().date()
    if not es_dia_laboral(d, False):
        d = siguiente_dia_laboral(d, False)
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute("UPDATE asignaciones SET fecha_plan=%s, hora_inicio_plan=%s WHERE id=%s",
                    (d.strftime("%Y-%m-%d"), "08:00", aid))
    else:
        cur.execute("UPDATE asignaciones SET fecha_plan=?, hora_inicio_plan=? WHERE id=?",
                    (d.strftime("%Y-%m-%d"), "08:00", aid))
    # mover este al final del dia elegido: put sort key late then repack whole operator
    # Get all, put this one after others with earlier fecha or same fecha
    if USE_POSTGRES:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=%s AND estado IN ('Pendiente','En proceso') ORDER BY fecha_plan, hora_inicio_plan, id",
            (operador,))
    else:
        cur.execute(
            "SELECT * FROM asignaciones WHERE operador=? AND estado IN ('Pendiente','En proceso') ORDER BY fecha_plan, hora_inicio_plan, id",
            (operador,))
    rows = as_dicts(cur.fetchall())
    others = [r for r in rows if r["id"] != aid]
    this = [r for r in rows if r["id"] == aid]
    ordered = others + this  # postponed goes last among same window; repack will place by fecha
    # Set provisional order: items keep relative order but this starts at chosen date
    base = now_mx().date()
    if not es_dia_laboral(base, False):
        base = siguiente_dia_laboral(base, False)
    for n, r in enumerate(others):
        hi = min_to_hhmm(8 * 60 + n)
        if USE_POSTGRES:
            cur.execute("UPDATE asignaciones SET fecha_plan=%s, hora_inicio_plan=%s WHERE id=%s",
                        (base.strftime("%Y-%m-%d"), hi, r["id"]))
        else:
            cur.execute("UPDATE asignaciones SET fecha_plan=?, hora_inicio_plan=? WHERE id=?",
                        (base.strftime("%Y-%m-%d"), hi, r["id"]))
    if this:
        hi = min_to_hhmm(8 * 60 + len(others))
        if USE_POSTGRES:
            cur.execute("UPDATE asignaciones SET fecha_plan=%s, hora_inicio_plan=%s WHERE id=%s",
                        (d.strftime("%Y-%m-%d"), hi, aid))
        else:
            cur.execute("UPDATE asignaciones SET fecha_plan=?, hora_inicio_plan=? WHERE id=?",
                        (d.strftime("%Y-%m-%d"), hi, aid))
    # Repack from earliest
    repack_operador(cur, operador, min(base.strftime("%Y-%m-%d"), d.strftime("%Y-%m-%d")))
    conn.commit(); conn.close()
    return redirect(url_for("cola_lista", operador=operador))

@app.route("/cola/reasignar", methods=["POST"])
@login_required
def cola_reasignar():
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin</div>', session)
    try:
        aid = int(request.form.get("id") or 0)
    except Exception:
        aid = 0
    old = (request.form.get("operador_old") or "").strip()
    new = (request.form.get("operador_new") or "").strip()
    if not aid or not new:
        return redirect(url_for("cola_lista", operador=old))
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute("UPDATE asignaciones SET operador=%s WHERE id=%s", (new, aid))
    else:
        cur.execute("UPDATE asignaciones SET operador=? WHERE id=?", (new, aid))
    repack_operador(cur, old)
    repack_operador(cur, new)
    conn.commit(); conn.close()
    return redirect(url_for("cola_lista", operador=new))

@app.route("/cola/repack", methods=["POST"])
@login_required
def cola_repack():
    if not is_admin_or_gerente():
        return page("Sin permiso", '<div class="alert alert-err">Solo gerente/admin</div>', session)
    operador = (request.form.get("operador") or "").strip()
    conn = get_db(); cur = dcur(conn)
    repack_operador(cur, operador)
    conn.commit(); conn.close()
    return redirect(url_for("cola_lista", operador=operador))


@app.route("/cola")
@login_required
def cola_gantt():
    """Gantt estilo Excel: filas = operador/trabajo, columnas = dias, barras cruzan fechas."""
    vista = (request.args.get("vista") or "semana").strip()  # dia | semana
    fecha = (request.args.get("fecha") or today_mx()).strip()
    try:
        ref = datetime.strptime(fecha[:10], "%Y-%m-%d").date()
    except Exception:
        ref = now_mx().date()
        fecha = ref.strftime("%Y-%m-%d")

    if vista == "dia":
        fechas = [fecha]
        titulo = "Detalle horario " + fecha
    else:
        vista = "semana"
        fi = ref - timedelta(days=ref.weekday())  # lunes
        fechas = [(fi + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]  # 2 semanas calendario
        titulo = "Gantt %s → %s" % (fechas[0], fechas[-1])

    f0, f1 = fechas[0], fechas[-1]
    conn = get_db(); cur = dcur(conn)
    if USE_POSTGRES:
        cur.execute(
            "SELECT a.*, t.vale, t.proyecto FROM asignaciones a "
            "JOIN trabajos t ON t.id=a.trabajo_id "
            "WHERE a.estado IN ('Pendiente','En proceso','Terminado') AND ("
            " (COALESCE(a.fecha_plan,'')<>'' AND a.fecha_plan<=%s AND COALESCE(NULLIF(a.fecha_fin_plan,''),a.fecha_plan)>=%s) "
            " OR (COALESCE(a.fecha_plan,'')=%s) "
            ") ORDER BY a.operador, a.fecha_plan, a.id",
            (f1, f0, f0))
    else:
        cur.execute(
            "SELECT a.*, t.vale, t.proyecto FROM asignaciones a "
            "JOIN trabajos t ON t.id=a.trabajo_id "
            "WHERE a.estado IN ('Pendiente','En proceso','Terminado') AND ("
            " (IFNULL(a.fecha_plan,'')<>'' AND a.fecha_plan<=? AND IFNULL(NULLIF(a.fecha_fin_plan,''),a.fecha_plan)>=?) "
            " OR (IFNULL(a.fecha_plan,'')=?) "
            ") ORDER BY a.operador, a.fecha_plan, a.id",
            (f1, f0, f0))
    asigs = as_dicts(cur.fetchall())
    if USE_POSTGRES:
        cur.execute("SELECT * FROM registros WHERE fecha_inicio>=%s AND fecha_inicio<=%s", (f0, f1))
    else:
        cur.execute("SELECT * FROM registros WHERE fecha_inicio>=? AND fecha_inicio<=?", (f0, f1))
    regs = as_dicts(cur.fetchall())
    conn.close()

    # Enrich with plan window
    items = []
    for a in asigs:
        pv = plan_ventana(a.get("fecha_plan"), a.get("hora_inicio_plan"),
                          a.get("minutos_plan"), a.get("sabado_autorizado"), a.get("velada_fechas"))
        fi_ = a.get("fecha_fin_plan") or pv["fecha_fin"]
        hi_ = a.get("hora_fin_plan") or pv["hora_fin"]
        items.append({
            "a": a,
            "pv": pv,
            "titulo": a.get("vale") or a.get("proyecto") or ("#" + str(a.get("trabajo_id"))),
            "operador": (a.get("operador") or "-").strip(),
            "maquina": a.get("maquina_id") or "",
            "fecha_ini": pv["fecha_ini"],
            "hora_ini": pv["hora_ini"],
            "fecha_fin": fi_,
            "hora_fin": (hi_ or "")[:5],
            "minutos": float(a.get("minutos_plan") or 0),
            "estado": a.get("estado") or "Pendiente",
            "segs": pv.get("segs") or [],
        })

    # Filter items overlapping fechas
    def overlaps(it):
        try:
            di = datetime.strptime(it["fecha_ini"][:10], "%Y-%m-%d").date()
            df = datetime.strptime(it["fecha_fin"][:10], "%Y-%m-%d").date()
            r0 = datetime.strptime(f0, "%Y-%m-%d").date()
            r1 = datetime.strptime(f1, "%Y-%m-%d").date()
            return di <= r1 and df >= r0
        except Exception:
            return True
    items = [it for it in items if overlaps(it)]

    def color_est(est):
        return {"Pendiente": "#4f46e5", "En proceso": "#d97706", "Terminado": "#059669"}.get(est, "#64748b")

    # ========== VISTA SEMANA: Gantt Excel (columnas = dias) ==========
    if vista == "semana":
        n = len(fechas)
        # header days
        th = "".join(
            '<th class="gday%s">%s<br><small>%s</small></th>' % (
                " gwe" if datetime.strptime(f, "%Y-%m-%d").weekday() >= 5 else "",
                datetime.strptime(f, "%Y-%m-%d").strftime("%d/%m"),
                ["Lu","Ma","Mi","Ju","Vi","Sa","Do"][datetime.strptime(f, "%Y-%m-%d").weekday()],
            ) for f in fechas)

        def bar_row(it, label_extra=""):
            try:
                di = datetime.strptime(it["fecha_ini"][:10], "%Y-%m-%d").date()
                df = datetime.strptime(it["fecha_fin"][:10], "%Y-%m-%d").date()
                r0 = datetime.strptime(f0, "%Y-%m-%d").date()
            except Exception:
                return "<tr><td colspan='%s'>-</td></tr>" % (n + 1)
            # column indices
            start_i = max(0, (di - r0).days)
            end_i = min(n - 1, (df - r0).days)
            if end_i < 0 or start_i > n - 1:
                return ""
            if start_i < 0:
                start_i = 0
            span = max(1, end_i - start_i + 1)
            cells = ""
            for i in range(0, start_i):
                wd = datetime.strptime(fechas[i], "%Y-%m-%d").weekday()
                cells += '<td class="gcell%s"></td>' % (" gwe" if wd >= 5 else "")
            tip = "%s | %s %s → %s %s | %.0f min | %s" % (
                it["titulo"], it["fecha_ini"], it["hora_ini"],
                it["fecha_fin"], it["hora_fin"], it["minutos"], it["estado"])
            wd0 = datetime.strptime(fechas[start_i], "%Y-%m-%d").weekday()
            cells += (
                '<td class="gcell%s" colspan="%s">'
                '<a class="gbar-xl" href="/trabajo/%s" title="%s" style="background:%s">'
                '<span>%s</span><small>%s → %s</small></a></td>'
            ) % (
                " gwe" if wd0 >= 5 else "", span, it["a"].get("trabajo_id"), tip.replace('"', "'"),
                color_est(it["estado"]),
                (it["titulo"][:22] + ("…" if len(it["titulo"]) > 22 else "")),
                it["hora_ini"], it["hora_fin"],
            )
            after = start_i + span
            for i in range(after, n):
                wd = datetime.strptime(fechas[i], "%Y-%m-%d").weekday()
                cells += '<td class="gcell%s"></td>' % (" gwe" if wd >= 5 else "")
            lab = it["operador"] + label_extra
            return (
                '<tr><td class="glabel"><strong>%s</strong><br>'
                '<small style="color:#64748b">%s · %.0f min</small></td>%s</tr>'
            ) % (lab, nombre_maq(it["maquina"]) if it["maquina"] else it["titulo"], it["minutos"], cells)

        # Group by operator
        by_op = {}
        for it in items:
            by_op.setdefault(it["operador"], []).append(it)

        rows_op = ""
        for op in sorted(by_op.keys()):
            for it in sorted(by_op[op], key=lambda x: (x["fecha_ini"], x["hora_ini"])):
                rows_op += bar_row(it)

        by_mq = {}
        for it in items:
            by_mq.setdefault(it["maquina"] or "-", []).append(it)
        rows_mq = ""
        for mq in sorted(by_mq.keys()):
            for it in sorted(by_mq[mq], key=lambda x: (x["fecha_ini"], x["hora_ini"])):
                rows_mq += bar_row(it, "")

        table_op = (
            '<div class="gwrap"><table class="gantt-xl">'
            '<thead><tr><th class="glabel">Operador / trabajo</th>%s</tr></thead>'
            '<tbody>%s</tbody></table></div>'
        ) % (th, rows_op or '<tr><td colspan="%s">Sin asignaciones en el horizonte</td></tr>' % (n + 1))

        table_mq = (
            '<div class="gwrap"><table class="gantt-xl">'
            '<thead><tr><th class="glabel">Maquina / trabajo</th>%s</tr></thead>'
            '<tbody>%s</tbody></table></div>'
        ) % (th, rows_mq or '<tr><td colspan="%s">Sin asignaciones</td></tr>' % (n + 1))

        body = (
            '<div class="card"><h1>Diagrama de Gantt</h1>'
            '<p style="color:#64748b;font-size:.9rem">Vista tipo Excel: cada barra cruza los dias del plan '
            "(inicio → fin). Fin de semana en gris. Veladas acortan el fin si las autorizaste.</p>"
            '<div style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0">'
            '<a class="btn btn-outline btn-sm" href="/cola?vista=dia&fecha=%s">Detalle horario (dia)</a> '
            '<a class="btn btn-primary btn-sm" href="/cola?vista=semana&fecha=%s">Gantt 2 semanas</a> '
            '<a class="btn btn-outline btn-sm" href="/cola/lista">Lista interactiva</a> '
            '<form method="GET" style="display:inline-flex;gap:.35rem;align-items:center">'
            '<input type="hidden" name="vista" value="semana">'
            '<input type="date" name="fecha" value="%s">'
            '<button class="btn btn-outline btn-sm">Ver</button></form>'
            '<span style="font-size:.85rem;color:#64748b">%s</span></div>'
            '<div class="gleg">'
            '<span><i style="background:#4f46e5"></i> Pendiente</span>'
            '<span><i style="background:#d97706"></i> En proceso</span>'
            '<span><i style="background:#059669"></i> Terminado</span></div></div>'
            '<div class="card"><h2>Por operador</h2>%s</div>'
            '<div class="card"><h2>Por maquina</h2>%s</div>'
            '%s'
        ) % (fecha, fecha, fecha, titulo, table_op, table_mq, _gantt_css())
        return page("Gantt", body, session)

    # ========== VISTA DIA: horario 8-18 ==========
    DAY_START, DAY_SPAN = 8 * 60, 10 * 60
    def block_style(start_min, dur_min, color):
        if dur_min <= 0:
            dur_min = 15
        left = max(0, (start_min - DAY_START) / DAY_SPAN * 100.0)
        width = max(1.2, dur_min / DAY_SPAN * 100.0)
        if left + width > 100:
            width = max(1.2, 100 - left)
        return "left:%.2f%%%%;width:%.2f%%%%;background:%s" % (left, width, color)

    lunch_left = (13 * 60 - DAY_START) / DAY_SPAN * 100.0
    lunch_w = 60 / DAY_SPAN * 100.0
    lunch = '<div class="glunch" style="left:%.2f%%%%;width:%.2f%%%%"></div>' % (lunch_left, lunch_w)

    def day_tracks(key_fn, key_name):
        groups = {}
        for it in items:
            k = key_fn(it) or "-"
            groups.setdefault(k, []).append(it)
        html = ""
        for key in sorted(groups.keys()):
            bars = ""
            total = 0
            for it in groups[key]:
                for s in it["segs"]:
                    if s[0].strftime("%Y-%m-%d") != fecha:
                        continue
                    dur = s[2] - s[1]
                    total += dur
                    tip = "%s %s-%s" % (it["titulo"], min_to_hhmm(s[1]), min_to_hhmm(s[2]))
                    bars += (
                        '<a class="gbar" href="/trabajo/%s" title="%s" style="%s">'
                        '<span class="gbar-t">%s</span></a>'
                    ) % (it["a"].get("trabajo_id"), tip.replace('"', "'"),
                         block_style(s[1], dur, color_est(it["estado"])),
                         it["titulo"][:14])
            if not bars:
                continue
            label = key if key_name == "operador" else nombre_maq(key)
            html += (
                '<div class="grow"><div class="glab"><strong>%s</strong><br>'
                '<span class="gmeta">%.0f min este dia</span></div>'
                '<div class="gtrack">%s%s</div></div>'
            ) % (label, total, lunch, bars)
        return html or '<p style="color:#94a3b8">Sin tramos este dia (prueba Gantt 2 semanas)</p>'

    ruler = "".join(
        '<span style="left:%.1f%%%%">%02d:00</span>' % ((h * 60 - DAY_START) / DAY_SPAN * 100.0, h)
        for h in range(8, 19)
    )
    body = (
        '<div class="card"><h1>Detalle horario del dia</h1>'
        '<p style="color:#64748b">Tramos de 08:00-18:00. Para ver barras de varios dias usa '
        '<strong>Gantt 2 semanas</strong> (estilo Excel).</p>'
        '<div style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0">'
        '<a class="btn btn-primary btn-sm" href="/cola?vista=dia&fecha=%s">Dia</a> '
        '<a class="btn btn-outline btn-sm" href="/cola?vista=semana&fecha=%s">Gantt 2 semanas</a> '
        '<a class="btn btn-outline btn-sm" href="/cola/lista">Lista</a> '
        '<form method="GET" style="display:inline-flex;gap:.35rem">'
        '<input type="hidden" name="vista" value="dia">'
        '<input type="date" name="fecha" value="%s">'
        '<button class="btn btn-outline btn-sm">Ver</button></form></div></div>'
        '<div class="card"><h2>Por operador</h2><div class="gruler">%s</div>%s</div>'
        '<div class="card"><h2>Por maquina</h2><div class="gruler">%s</div>%s</div>%s'
    ) % (
        fecha, fecha, fecha, ruler,
        day_tracks(lambda it: it["operador"], "operador"),
        ruler,
        day_tracks(lambda it: it["maquina"], "maquina"),
        _gantt_css(),
    )
    return page("Gantt dia", body, session)

def _gantt_css():
    return (
        '<style>'
        ".gwrap{overflow-x:auto}"
        ".gantt-xl{border-collapse:collapse;width:100%;min-width:900px;font-size:.78rem}"
        ".gantt-xl th,.gantt-xl td{border:1px solid #e2e8f0;padding:0;vertical-align:middle}"
        ".gantt-xl thead th{background:#f1f5f9;text-align:center;padding:.35rem .2rem;font-weight:600;color:#475569}"
        ".gantt-xl th.gwe,.gantt-xl td.gwe{background:#f8fafc}"
        ".gantt-xl .glabel{min-width:140px;max-width:180px;padding:.4rem .5rem;text-align:left;background:#fff;position:sticky;left:0;z-index:2;border-right:2px solid #cbd5e1}"
        ".gantt-xl thead th.glabel{z-index:3}"
        ".gcell{height:42px;min-width:48px}"
        ".gbar-xl{display:flex;flex-direction:column;justify-content:center;height:32px;margin:4px 3px;border-radius:6px;"
        "color:#fff;padding:0 6px;text-decoration:none;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.12)}"
        ".gbar-xl span{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
        ".gbar-xl small{opacity:.9;font-size:.65rem}"
        ".gleg{display:flex;flex-wrap:wrap;gap:.8rem;font-size:.75rem;margin-top:.5rem}"
        ".gleg i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:4px;vertical-align:middle}"
        ".gruler{position:relative;height:1.2rem;margin-left:160px;margin-bottom:.3rem;font-size:.65rem;color:#64748b}"
        ".gruler span{position:absolute;transform:translateX(-50%%)}"
        ".grow{display:flex;align-items:stretch;margin:.35rem 0;gap:.4rem}"
        ".glab{width:150px;flex-shrink:0;font-size:.8rem}"
        ".gmeta{font-size:.68rem;color:#64748b}"
        ".gtrack{position:relative;flex:1;height:36px;background:#f1f5f9;border-radius:8px;overflow:hidden;border:1px solid #e2e8f0}"
        ".gbar{position:absolute;top:3px;bottom:3px;border-radius:6px;color:#fff;font-size:.65rem;"
        "display:flex;align-items:center;padding:0 4px;overflow:hidden;text-decoration:none;z-index:2}"
        ".gbar-t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
        ".glunch{position:absolute;top:0;bottom:0;background:#fde68a;opacity:.85;z-index:1}"
        "</style>"
    )


@app.route("/productividad")
@login_required
def productividad():
    periodo = (request.args.get("periodo") or "semana").strip()
    fecha_ref = (request.args.get("fecha") or today_mx()).strip()
    try:
        ref = datetime.strptime(fecha_ref[:10], "%Y-%m-%d").date()
    except Exception:
        ref = now_mx().date()
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
    regs = as_dicts(cur.fetchall())
    cur.execute(
        "SELECT * FROM ponchados WHERE fecha_inicio>=" + ph() + " AND fecha_inicio<=" + ph() + " ORDER BY fecha_inicio, id",
        (fi_s, ff_s))
    ponchs = as_dicts(cur.fetchall()); conn.close()
    by_op, by_maq = {}, {}
    tot_pzas = tot_punt = tot_j = tot_v = tot_inc = 0
    tot_ponch = tot_punt_ponch = tot_min_ponch = 0
    for r in regs:
        op = (r.get("operador") or "-").strip()
        mid = r.get("maquina_id") or "?"
        pzas = int(r.get("piezas") or 0)
        pu = int(r.get("puntadas_unit") or 0)
        punt = pzas * pu
        mt = minutos_trabajados(r.get("fecha_inicio"), r.get("hora_inicio"), r.get("fecha_fin"), r.get("hora_fin"), velada_flag(r))
        cab = cabezas_maq(mid)
        mins_proy = float(r.get("minutos_proyectados") or 0)
        vueltas = (pzas / cab) if cab else pzas
        proy = vueltas * mins_proy
        has_inc = 1 if (r.get("incidencia") or "").strip() else 0
        for bucket, key in ((by_op, op), (by_maq, mid)):
            st = bucket.setdefault(key, {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "proy": 0.0, "regs": 0, "inc": 0, "ponch": 0})
            st["pzas"] += pzas; st["punt"] += punt
            st["jornada"] += mt["jornada"]; st["velada"] += mt["velada"]; st["total"] += mt["total"]
            st["proy"] += proy; st["regs"] += 1; st["inc"] += has_inc
        tot_pzas += pzas; tot_punt += punt; tot_j += mt["jornada"]; tot_v += mt["velada"]; tot_inc += has_inc

    # Ponchados suman a productividad del usuario (tiempo + puntadas)
    for p in ponchs:
        user = (p.get("usuario") or "-").strip()
        punt = int(p.get("puntadas") or 0)
        mt = minutos_trabajados(p.get("fecha_inicio"), p.get("hora_inicio"), p.get("fecha_fin"), p.get("hora_fin"), velada_flag(p))
        st = by_op.setdefault(user, {"pzas": 0, "punt": 0, "jornada": 0, "velada": 0, "total": 0, "proy": 0.0, "regs": 0, "inc": 0, "ponch": 0})
        st["punt"] += punt
        st["jornada"] += mt["jornada"]
        st["velada"] += mt["velada"]
        st["total"] += mt["total"]
        st["ponch"] = st.get("ponch", 0) + 1
        st["regs"] += 1  # cuenta como actividad
        tot_punt += punt
        tot_j += mt["jornada"]
        tot_v += mt["velada"]
        tot_ponch += 1
        tot_punt_ponch += punt
        tot_min_ponch += mt["total"]

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
        '<div class="stat"><div class="n">%s</div><div class="l">Incidencias</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Ponchados</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Puntadas ponchado</div></div>'
        '<div class="stat"><div class="n">%s</div><div class="l">Min ponchado</div></div></div></div>'
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
        "Jornada resta comida 14-15. Velada 18:00-02:00 solo si se autoriza. Sabado 8:00-17:30. Los datos NO se borran al actualizar codigo si usas Postgres (DATABASE_URL).</p></div>"
    ) % (
        titulo, tabs, periodo, fecha_ref,
        tot_pzas, "{:,}".format(tot_punt), tot_j, tot_v, tot_j + tot_v, tot_inc,
        tot_ponch, "{:,}".format(tot_punt_ponch), tot_min_ponch,
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
