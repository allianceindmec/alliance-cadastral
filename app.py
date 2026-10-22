import os
import json
import secrets
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, abort, send_file)
import io
import zipfile

import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)
API_KEY      = os.environ.get("API_KEY", "rh-api-key-change-in-prod")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ------------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------------

def get_db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens_pendentes (
                id          SERIAL PRIMARY KEY,
                token       TEXT UNIQUE NOT NULL,
                filial      TEXT NOT NULL,
                mat         TEXT NOT NULL,
                dados       JSONB NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.execute("""
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
        conn.execute("""
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


# ------------------------------------------------------------------
# Auth decorator
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
    # Busca dados do token no banco (novo sistema de tokens curtos)
    with get_db() as conn:
        tp = conn.execute(
            "SELECT dados FROM tokens_pendentes WHERE token=%s", (token,)
        ).fetchone()
    if tp is None:
        return render_template("invalido.html"), 400
    dados = tp[0] if isinstance(tp[0], dict) else json.loads(tp[0])

    if request.method == "GET":
        with get_db() as conn:
            row = conn.execute("SELECT id FROM respostas WHERE token=%s", (token,)).fetchone()
            if row:
                return render_template("ja_respondido.html", nome=dados.get("nome", ""))
        return render_template("form.html", d=dados, token=token)

    # POST — salvar resposta
    form  = request.form
    files = request.files

    dados_resp = {
        "nome":         form.get("nome", ""),
        "cpf":          form.get("cpf", ""),
        "rg":           form.get("rg", ""),
        "telefone":     form.get("telefone", ""),
        "email":        form.get("email", ""),
        "estado_civil": form.get("estado_civil", ""),
        "conjuge": {
            "nome":        form.get("conjuge_nome", ""),
            "cpf":         form.get("conjuge_cpf", ""),
            "rg":          form.get("conjuge_rg", ""),
            "dt_nasc":     form.get("conjuge_dt_nasc", ""),
            "cidade_nasc": form.get("conjuge_cidade_nasc", ""),
        },
        "cep":          form.get("cep", ""),
        "endereco":     form.get("endereco", ""),
        "numero":       form.get("numero", ""),
        "complemento":  form.get("complemento", ""),
        "bairro":       form.get("bairro", ""),
        "cidade":       form.get("cidade", ""),
        "estado":       form.get("estado", ""),
        "contato_emergencial": {
            "nome":       form.get("contato_emerg_nome", ""),
            "telefone":   form.get("contato_emerg_tel", ""),
            "parentesco": form.get("contato_emerg_parentesco", ""),
        },
        "grau_escolaridade": form.get("grau_escolaridade", ""),
        "cursos":        json.loads(form.get("cursos_json", "[]")),
        "dependentes":   json.loads(form.get("dependentes_json", "[]")),
    }

    CAMPOS_ARQUIVO = ["foto_rosto", "comprovante_endereco", "cert_casamento", "cert_obito",
                      "doc_cpf", "doc_rg_frente", "doc_rg_verso", "doc_cert_nasc_func"]
    for i in range(10):
        CAMPOS_ARQUIVO.append(f"cert_nasc_{i}")
    for i in range(20):
        CAMPOS_ARQUIVO.append(f"cert_curso_{i}")

    with get_db() as conn:
        row = conn.execute("""
            INSERT INTO respostas (token, filial, mat, nome, dados)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (token) DO NOTHING
            RETURNING id
        """, (token, dados.get("filial"), dados.get("mat"),
              dados_resp["nome"], json.dumps(dados_resp))).fetchone()

        if row is None:
            return render_template("ja_respondido.html", nome=dados_resp["nome"])

        resp_id = row[0]

        for campo in CAMPOS_ARQUIVO:
            f = files.get(campo)
            if f and f.filename:
                raw = f.read()
                if raw:
                    conn.execute("""
                        INSERT INTO arquivos (resposta_id, campo, nome_orig, mime, dados)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (resp_id, campo, f.filename, f.content_type, raw))

    return redirect(url_for("obrigado"))


@app.route("/obrigado")
def obrigado():
    return render_template("obrigado.html")


# ------------------------------------------------------------------
# Internal API
# ------------------------------------------------------------------

@app.route("/api/respostas")
@api_auth
def api_respostas():
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute("""
                SELECT r.id, r.filial, r.mat, r.nome,
                       r.dados, r.created_at,
                       COUNT(a.id) AS qtd_arquivos,
                       JSON_AGG(
                           JSON_BUILD_OBJECT('id', a.id, 'campo', a.campo, 'nome', a.nome_orig, 'mime', a.mime)
                       ) FILTER (WHERE a.id IS NOT NULL) AS arquivos
                FROM respostas r
                LEFT JOIN arquivos a ON a.resposta_id = r.id
                GROUP BY r.id
                ORDER BY r.created_at DESC
            """).fetchall()
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
        row = conn.execute(
            "SELECT nome_orig, mime, dados FROM arquivos WHERE id=%s", (arq_id,)
        ).fetchone()
    if not row:
        abort(404)
    nome_orig, mime, dados = row
    return send_file(io.BytesIO(bytes(dados)), mimetype=mime,
                     download_name=nome_orig, as_attachment=True)


@app.route("/api/export_zip")
@api_auth
def api_export_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with get_db() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                respostas = cur.execute(
                    "SELECT * FROM respostas ORDER BY id"
                ).fetchall()
            arquivos = conn.execute("SELECT * FROM arquivos").fetchall()

        resumo = [{k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                   for k, v in r.items()} for r in respostas]
        zf.writestr("respostas.json", json.dumps(resumo, ensure_ascii=False, indent=2))

        for arq in arquivos:
            folder = f"{arq[1]}_{arq[3]}"
            zf.writestr(f"arquivos/{folder}/{arq[4]}", bytes(arq[6]))

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return send_file(buf, mimetype="application/zip",
                     download_name=f"cadastral_export_{ts}.zip", as_attachment=True)


@app.route("/api/resposta/<int:resp_id>")
@api_auth
def api_resposta_single(resp_id):
    with get_db() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("""
                SELECT r.id, r.filial, r.mat, r.nome,
                       r.dados, r.created_at,
                       COUNT(a.id) AS qtd_arquivos,
                       JSON_AGG(
                           JSON_BUILD_OBJECT('id', a.id, 'campo', a.campo,
                                             'nome', a.nome_orig, 'mime', a.mime)
                       ) FILTER (WHERE a.id IS NOT NULL) AS arquivos
                FROM respostas r
                LEFT JOIN arquivos a ON a.resposta_id = r.id
                WHERE r.id = %s
                GROUP BY r.id
            """, (resp_id,)).fetchone()
    if not row:
        abort(404)
    d = dict(row)
    d["created_at"] = d["created_at"].isoformat() if d["created_at"] else None
    return jsonify(d)


@app.route("/api/limpar_arquivos/<int:resp_id>", methods=["POST"])
@api_auth
def api_limpar_arquivos(resp_id):
    """Apaga o binário dos arquivos de uma resposta já baixada, mantendo o registro."""
    with get_db() as conn:
        resultado = conn.execute(
            "UPDATE arquivos SET dados = '\\x'::bytea WHERE resposta_id = %s AND length(dados) > 0",
            (resp_id,)
        )
        conn.commit()
    return jsonify({"ok": True, "resp_id": resp_id})


@app.route("/api/responderam")
@api_auth
def api_responderam():
    """Retorna lista de {mat, filial} de quem já respondeu, opcionalmente filtrado por filial."""
    filial = request.args.get("filial")
    with get_db() as conn:
        if filial:
            rows = conn.execute(
                "SELECT mat, filial FROM respostas WHERE filial=%s", (filial,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT mat, filial FROM respostas").fetchall()
    return jsonify([{"mat": r[0], "filial": r[1]} for r in rows])


@app.route("/api/pendentes")
@api_auth
def api_pendentes():
    mats   = request.args.getlist("mat")
    filial = request.args.get("filial", "")
    if not mats:
        return jsonify([])
    placeholders = ",".join(["%s"] * len(mats))
    params = mats + ([filial] if filial else [])
    filial_clause = " AND filial=%s" if filial else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT mat FROM respostas WHERE mat IN ({placeholders}){filial_clause}", params
        ).fetchall()
    responderam = {r[0] for r in rows}
    return jsonify([m for m in mats if m not in responderam])


@app.route("/api/criar_token", methods=["POST"])
@api_auth
def api_criar_token():
    """Registra dados de um funcionário e retorna um token curto."""
    body = request.get_json(silent=True)
    if not body or "mat" not in body or "filial" not in body:
        abort(400)
    mat    = str(body["mat"]).strip()
    filial = str(body["filial"]).strip()
    with get_db() as conn:
        row = conn.execute(
            "SELECT token FROM tokens_pendentes WHERE mat=%s AND filial=%s", (mat, filial)
        ).fetchone()
        if row:
            return jsonify({"token": row[0]})
        for _ in range(10):
            token = secrets.token_urlsafe(8)
            if not conn.execute("SELECT 1 FROM tokens_pendentes WHERE token=%s", (token,)).fetchone():
                break
        conn.execute(
            "INSERT INTO tokens_pendentes (token, filial, mat, dados) VALUES (%s,%s,%s,%s)",
            (token, filial, mat, json.dumps(body, ensure_ascii=False))
        )
    return jsonify({"token": token})


@app.route("/api/criar_tokens_lote", methods=["POST"])
@api_auth
def api_criar_tokens_lote():
    """Registra uma lista de funcionários em uma única transação. Retorna [{mat, filial, token}]."""
    lista = request.get_json(silent=True)
    if not isinstance(lista, list):
        abort(400)

    resultado = []
    with get_db() as conn:
        # Busca todos os tokens existentes para (mat, filial) do lote de uma vez
        pares = [(str(d["mat"]).strip(), str(d["filial"]).strip()) for d in lista if "mat" in d and "filial" in d]
        if not pares:
            return jsonify([])

        # Tokens já existentes
        existentes = {}
        if pares:
            ph = ",".join(["(%s,%s)"] * len(pares))
            flat = [v for p in pares for v in p]
            rows = conn.execute(
                f"SELECT mat, filial, token FROM tokens_pendentes WHERE (mat, filial) IN ({ph})", flat
            ).fetchall()
            for r in rows:
                existentes[(r[0], r[1])] = r[2]

        # Para os que não têm token, inserir em lote
        novos = []
        for d in lista:
            if "mat" not in d or "filial" not in d:
                continue
            mat    = str(d["mat"]).strip()
            filial = str(d["filial"]).strip()
            if (mat, filial) in existentes:
                resultado.append({"mat": mat, "filial": filial, "token": existentes[(mat, filial)]})
            else:
                for _ in range(10):
                    token = secrets.token_urlsafe(8)
                    if token not in existentes.values():
                        break
                novos.append((token, filial, mat, json.dumps(d, ensure_ascii=False)))
                existentes[(mat, filial)] = token
                resultado.append({"mat": mat, "filial": filial, "token": token})

        if novos:
            conn.executemany(
                "INSERT INTO tokens_pendentes (token, filial, mat, dados) VALUES (%s,%s,%s,%s) ON CONFLICT (token) DO NOTHING",
                novos
            )

    return jsonify(resultado)


# ------------------------------------------------------------------
# Startup
# ------------------------------------------------------------------

# Inicializa o banco sempre que o app sobe
if DATABASE_URL:
    try:
        init_db()
    except Exception as e:
        print(f"[init_db] {e}")


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
