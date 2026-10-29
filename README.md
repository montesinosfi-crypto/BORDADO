# Sistema de Bordado

## Archivos obligatorios en GitHub (raiz del repo)

- `app.py`  (NO aplicacion.py)
- `wsgi.py`
- `requirements.txt`  (NO requisitos.txt)
- `Procfile`

Start Command en Render:
`gunicorn wsgi:app --workers 1 --threads 2 --timeout 120 --bind 0.0.0.0:$PORT`

DATABASE_URL = Internal URL de PostgreSQL

Usuarios: admin/admin123, gerente/gerente123, operador/operador123
