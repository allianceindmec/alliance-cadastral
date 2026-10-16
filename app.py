import os
import json
import hmac
import hashlib
import base64
import psycopg2
import psycopg2.extras
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, abort, send_file)
import io
import zipfile

app = Flask(__name__)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
API_KEY    = os.environ.get("API_KEY", "rh-api-key-change-in-prod")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ------------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------------

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS respostas (
                    id          SERIAL PRIMARY KEY,
                    token       TEXT UNIQUE NOT NULL,
                    filial      TEXT,
                    mat         TEXT,
                    nome        TEXT,
                    dados       JSONB,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS arquivos (
                    id          SERIAL PRIMARY KEY,
                    resposta_id INTEGER REFERENCES respostas(id) ON DELETE CASCADE,
                    campo       TEXT NOT NULL,
                    nome_orig   TEXT,
                    mime        TEXT,
                    dados       BYTEA NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        conn.commit()


# ------------------------------------------------------------------
# Token helpers
# ------------------------------------------------------------------

def _sign(payload: str) -> str:
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]


def gerar_token(dados: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(dados, ensure_ascii=False).encode()).decode()
    sig = _sign(payload)
    return f"{payload}.{sig}"


def verificar_token(token: str):
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        return json.loads(base64.urlsafe_b64decode(payload + "=="))
    except Exception:
        return None


# ------------------------------------------------------------------
# Auth decorator for internal API
# ------------------------------------------------------------------

def api_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if key != API_KEY:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/")
def index():
    return "Formulário de Atualização Cadastral — Alliance", 200


@app.route("/atualizar/<token>", methods=["GET", "POST"])
def formulario(token):
    dados = verificar_token(token)
    if dados is None:
        return render_template("invalido.html"), 400

    if request.method == "GET":
        # Check if already submitted
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT id FROM respostas WHERE token=%s", (token,))
                if c.fetchone():
                    return render_template("ja_respondido.html", nome=dados.get("nome", ""))
        return render_template("form.html", d=dados, token=token)

    # POST — save response
    form = request.form
    files = request.files

    dados_resp = {
        "nome":          form.get("nome", ""),
        "cpf":           form.get("cpf", ""),
        "rg":            form.get("rg", ""),
        "telefone":      form.get("telefone", ""),
        "email":         form.get("email", ""),
        "estado_civil":  form.get("estado_civil", ""),
        "cep":           form.get("cep", ""),
        "endereco":      form.get("endereco", ""),
        "numero":        form.get("numero", ""),
        "complemento":   form.get("complemento", ""),
        "bairro":        form.get("bairro", ""),
        "cidade":        form.get("cidade", ""),
        "estado":        form.get("estado", ""),
        "dependentes":   json.loads(form.get("dependentes_json", "[]")),
    }

    CAMPOS_ARQUIVO = [
        "foto_rosto",
        "comprovante_endereco",
        "cert_casamento",
    ]
    # dependents' certs: cert_nasc_0, cert_nasc_1, ...
    for i in range(10):
        CAMPOS_ARQUIVO.append(f"cert_nasc_{i}")

    with get_db() as conn:
        with conn.cursor() as c:
            # Upsert — ignore if already exists
            c.execute("""
                INSERT INTO respostas (token, filial, mat, nome, dados)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (token) DO NOTHING
                RETURNING id
            """, (token, dados.get("filial"), dados.get("mat"), dados_resp["nome"],
                  json.dumps(dados_resp)))
            row = c.fetchone()
            if row is None:
                return render_template("ja_respondido.html", nome=dados_resp["nome"])
            resp_id = row[0]

            for campo in CAMPOS_ARQUIVO:
                f = files.get(campo)
                if f and f.filename:
                    raw = f.read()
                    if raw:
                        c.execute("""
                            INSERT INTO arquivos (resposta_id, campo, nome_orig, mime, dados)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (resp_id, campo, f.filename, f.content_type, psycopg2.Binary(raw)))
        conn.commit()

    return redirect(url_for("obrigado"))


@app.route("/obrigado")
def obrigado():
    return render_template("obrigado.html")


# ------------------------------------------------------------------
# Internal API (called by main dashboard)
# ------------------------------------------------------------------

@app.route("/api/respostas")
@api_auth
def api_respostas():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute("""
                SELECT r.id, r.filial, r.mat, r.nome,
                       r.dados, r.created_at,
                       COUNT(a.id) AS qtd_arquivos
                FROM respostas r
                LEFT JOIN arquivos a ON a.resposta_id = r.id
                GROUP BY r.id
                ORDER BY r.created_at DESC
            """)
            rows = c.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
        result.append(d)
    return jsonify(result)


@app.route("/api/arquivo/<int:arq_id>")
@api_auth
def api_arquivo(arq_id):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT nome_orig, mime, dados FROM arquivos WHERE id=%s", (arq_id,))
            row = c.fetchone()
    if not row:
        abort(404)
    nome_orig, mime, dados = row
    return send_file(io.BytesIO(bytes(dados)), mimetype=mime,
                     download_name=nome_orig, as_attachment=True)


@app.route("/api/export_zip")
@api_auth
def api_export_zip():
    """Download all responses + files as a ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
                c.execute("SELECT * FROM respostas ORDER BY id")
                respostas = c.fetchall()
                c.execute("SELECT * FROM arquivos")
                arquivos = c.fetchall()

        resumo = []
        for r in respostas:
            d = dict(r)
            d["created_at"] = str(d["created_at"])
            resumo.append(d)
        zf.writestr("respostas.json", json.dumps(resumo, ensure_ascii=False, indent=2, default=str))

        for arq in arquivos:
            folder = f"{arq['resposta_id']}_{arq['campo']}"
            zf.writestr(f"arquivos/{folder}/{arq['nome_orig']}", bytes(arq['dados']))

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return send_file(buf, mimetype="application/zip",
                     download_name=f"cadastral_export_{ts}.zip", as_attachment=True)


@app.route("/api/pendentes")
@api_auth
def api_pendentes():
    """List mats that have NOT responded yet."""
    mats = request.args.getlist("mat")
    if not mats:
        return jsonify([])
    with get_db() as conn:
        with conn.cursor() as c:
            placeholders = ",".join(["%s"] * len(mats))
            c.execute(f"SELECT mat FROM respostas WHERE mat IN ({placeholders})", mats)
            responderam = {row[0] for row in c.fetchall()}
    pendentes = [m for m in mats if m not in responderam]
    return jsonify(pendentes)


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------

if __name__ == "__main__":
    if DATABASE_URL:
        init_db()
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
