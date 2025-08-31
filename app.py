# ==========================================
# Guarita - Controle de Chaves (Completo + Autorizações + Categorias + Atraso 23h)
# ==========================================
import os, io, uuid, sqlite3, datetime, zipfile
from typing import Optional, Tuple, List
import pandas as pd
import streamlit as st
from PIL import Image
from streamlit_drawable_canvas import st_canvas
import qrcode
import sqlite3 as _sqlite3  # capturar IntegrityError

# -------------- Configurações --------------
st.set_page_config(page_title="Guarita - Controle de Chaves", layout="wide")
APP_TITLE = "Guarita – Controle de Chaves"

ADMIN_PASS = st.secrets.get("STREAMLIT_ADMIN_PASS", os.getenv("STREAMLIT_ADMIN_PASS", ""))
SECRET_BASE_URL = st.secrets.get("BASE_URL", os.getenv("BASE_URL", "")).strip()
DB_PATH = os.getenv("DB_PATH", "keys.db")

# Corte para atraso "até 23:00 do dia" (padrão 23)
CUTOFF_HOUR_FOR_OVERDUE = int(os.getenv("CUTOFF_HOUR_FOR_OVERDUE", "23"))

# -------------- Utilidades -----------------
def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")

def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

def make_qr(data: str) -> Image.Image:
    qr = qrcode.QRCode(version=2, box_size=8, border=2)
    qr.add_data(data); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img.convert("RGB")

def build_url(base_url: str, params: dict) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return ""
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None and v != "")
    return f"{base}/?{query}" if query else f"{base}/"

# -------------- Banco de Dados -------------
def conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""PRAGMA foreign_keys = ON;""")
    # spaces (adicionamos category por migração)
    c.execute("""
      CREATE TABLE IF NOT EXISTS spaces(
        key_number INTEGER PRIMARY KEY,
        room_name  TEXT NOT NULL,
        location   TEXT,
        is_active  INTEGER DEFAULT 1
      )
    """)
    # MIGRAÇÃO: coluna category
    try:
        c.execute("ALTER TABLE spaces ADD COLUMN category TEXT DEFAULT 'Sala'")
    except Exception:
        pass

    c.execute("""
      CREATE TABLE IF NOT EXISTS persons(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        id_code TEXT,
        phone TEXT,
        is_active INTEGER DEFAULT 1
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS transactions(
        id TEXT PRIMARY KEY,
        key_number INTEGER NOT NULL,
        taken_by_name TEXT NOT NULL,
        taken_by_id   TEXT,
        taken_phone   TEXT,
        checkout_time TEXT NOT NULL,
        due_time      TEXT,
        checkin_time  TEXT,
        status        TEXT,             -- EM_USO / DEVOLVIDA
        signature_out BLOB,
        signature_in  BLOB,
        FOREIGN KEY (key_number) REFERENCES spaces(key_number)
      )
    """)
    # Autorizações
    c.execute("""
      CREATE TABLE IF NOT EXISTS authorizations(
        id TEXT PRIMARY KEY,
        key_number INTEGER NOT NULL,
        memo_number TEXT,
        valid_from TEXT,
        valid_to   TEXT,
        created_at TEXT,
        FOREIGN KEY (key_number) REFERENCES spaces(key_number)
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS authorization_people(
        id TEXT PRIMARY KEY,
        authorization_id TEXT NOT NULL,
        person_id TEXT NOT NULL,
        FOREIGN KEY (authorization_id) REFERENCES authorizations(id),
        FOREIGN KEY (person_id) REFERENCES persons(id)
      )
    """)
    return c

# ----- Helpers: Spaces -----
def add_space(key_number: int, room_name: str, location: str = "", category: str = "Sala"):
    c = conn()
    with c:
        c.execute("""INSERT OR REPLACE INTO spaces(key_number,room_name,location,is_active,category)
                     VALUES(?,?,?,?,?)""",
                  (key_number, room_name, location, 1, category))

def list_spaces(active_only=True):
    c = conn()
    if active_only:
        return pd.read_sql_query("SELECT * FROM spaces WHERE is_active=1 ORDER BY key_number", c)
    return pd.read_sql_query("SELECT * FROM spaces ORDER BY key_number", c)

def update_space(key_number: int, room_name: str, location: str, is_active: int, category: str = "Sala"):
    c = conn()
    with c:
        c.execute("""UPDATE spaces SET room_name=?, location=?, is_active=?, category=? WHERE key_number=?""",
                  (room_name, location, int(is_active), category, key_number))

def space_exists_and_active(key_number: int) -> bool:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT 1 FROM spaces WHERE key_number=? AND is_active=1", (key_number,))
    return cur.fetchone() is not None

# ----- Helpers: Persons -----
def add_person(name: str, id_code: str = "", phone: str = ""):
    c = conn()
    with c:
        c.execute("INSERT INTO persons(id,name,id_code,phone,is_active) VALUES(?,?,?,?,1)",
                  (str(uuid.uuid4()), name, id_code, phone))

def list_persons(active_only=True):
    c = conn()
    if active_only:
        return pd.read_sql_query("SELECT * FROM persons WHERE is_active=1 ORDER BY name", c)
    return pd.read_sql_query("SELECT * FROM persons ORDER BY name", c)

def update_person(pid: str, name: str, id_code: str, phone: str, is_active: int):
    c = conn()
    with c:
        c.execute("UPDATE persons SET name=?, id_code=?, phone=?, is_active=? WHERE id=?",
                  (name, id_code, phone, int(is_active), pid))

# ----- Helpers: Autorizações -----
def add_authorization(key_number:int, memo_number:str, valid_from:Optional[datetime.date], valid_to:Optional[datetime.date]) -> str:
    c = conn(); aid = str(uuid.uuid4())
    with c:
        c.execute("""INSERT INTO authorizations(id,key_number,memo_number,valid_from,valid_to,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (aid, key_number, memo_number,
                   datetime.datetime.combine(valid_from, datetime.time.min).isoformat(timespec="seconds") if valid_from else None,
                   datetime.datetime.combine(valid_to,   datetime.time.max).isoformat(timespec="seconds") if valid_to   else None,
                   now_iso()))
    return aid

def list_authorizations(key_number:int=None) -> pd.DataFrame:
    c = conn()
    q = "SELECT * FROM authorizations"
    p = []
    if key_number is not None:
        q += " WHERE key_number=?"
        p.append(key_number)
    q += " ORDER BY created_at DESC"
    return pd.read_sql_query(q, c, params=p)

def add_person_to_authorization(authorization_id:str, person_id:str):
    c = conn()
    with c:
        c.execute("""INSERT INTO authorization_people(id,authorization_id,person_id)
                     VALUES(?,?,?)""", (str(uuid.uuid4()), authorization_id, person_id))

def list_authorized_people_now(key_number:int) -> pd.DataFrame:
    """Retorna pessoas autorizadas para esta chave no momento atual."""
    c = conn()
    now = now_iso()
    q = """
    SELECT p.*
    FROM persons p
    JOIN authorization_people ap ON ap.person_id = p.id
    JOIN authorizations a ON a.id = ap.authorization_id
    WHERE a.key_number = ?
      AND (a.valid_from IS NULL OR datetime(a.valid_from) <= datetime(?))
      AND (a.valid_to   IS NULL OR datetime(a.valid_to)   >= datetime(?))
      AND p.is_active = 1
    """
    return pd.read_sql_query(q, c, params=[key_number, now, now])

# ----- Operação / Transactions -----
def has_open_checkout(key_number: int) -> bool:
    c = conn()
    cur = c.cursor()
    cur.execute("""SELECT 1 FROM transactions 
                   WHERE key_number=? AND checkin_time IS NULL
                   LIMIT 1""", (key_number,))
    return cur.fetchone() is not None

def open_checkout(key_number: int, name: str, id_code: str, phone: str,
                  due_time: Optional[datetime.datetime], signature_png: Optional[bytes]) -> Tuple[bool, str]:
    if not space_exists_and_active(key_number):
        return False, f"A chave {key_number} não está cadastrada como ATIVA. Cadastre/ative em Cadastros → Espaços."
    name = (name or "").strip()
    if not name:
        return False, "Informe o nome de quem está retirando a chave."
    if has_open_checkout(key_number):
        return False, "Esta chave já está EM USO. Faça a devolução antes de nova retirada."
    c = conn()
    tid = str(uuid.uuid4())
    try:
        with c:
            c.execute("""INSERT INTO transactions
                         (id,key_number,taken_by_name,taken_by_id,taken_phone,checkout_time,due_time,checkin_time,status,signature_out,signature_in)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (tid, key_number, name, (id_code or "").strip(), (phone or "").strip(),
                       now_iso(),
                       due_time.isoformat(timespec="seconds") if due_time else None,
                       None, "EM_USO", signature_png, None))
        return True, tid
    except _sqlite3.IntegrityError:
        return False, "Não foi possível registrar a retirada. Verifique se a chave existe/está ativa e os campos obrigatórios."

def do_checkin(key_number: int, signature_png: Optional[bytes]) -> Tuple[bool, str]:
    if not space_exists_and_active(key_number):
        return False, f"A chave {key_number} não está cadastrada/ativa. Cadastre/ative em Cadastros → Espaços."
    c = conn()
    cur = c.cursor()
    cur.execute("""SELECT id FROM transactions 
                   WHERE key_number=? AND checkin_time IS NULL
                   ORDER BY checkout_time DESC LIMIT 1""", (key_number,))
    row = cur.fetchone()
    if not row:
        return False, "Não há retirada em aberto para esta chave."
    tid = row[0]
    with c:
        c.execute("""UPDATE transactions SET checkin_time=?, status=?, signature_in=? WHERE id=?""",
                  (now_iso(), "DEVOLVIDA", signature_png, tid))
    return True, tid

def list_status() -> pd.DataFrame:
    c = conn()
    df_space = list_spaces(active_only=True)  # inclui category
    df_tx = pd.read_sql_query("""
        SELECT t.key_number, t.checkout_time, t.due_time, t.checkin_time, t.status AS last_status
        FROM transactions t
        INNER JOIN (
          SELECT key_number, MAX(checkout_time) AS max_co FROM transactions GROUP BY key_number
        ) m ON t.key_number=m.key_number AND t.checkout_time=m.max_co
    """, c)
    df = df_space.merge(df_tx, on="key_number", how="left")

    def compute_status(row):
        if pd.isna(row["checkout_time"]):
            return "DISPONÍVEL"
        if pd.isna(row["checkin_time"]):
            now = datetime.datetime.now()
            if pd.notna(row["due_time"]):
                try:
                    due = datetime.datetime.fromisoformat(str(row["due_time"]))
                    if now > due:
                        return "ATRASADA"
                except Exception:
                    pass
            try:
                co = datetime.datetime.fromisoformat(str(row["checkout_time"]))
                limit = co.replace(hour=CUTOFF_HOUR_FOR_OVERDUE, minute=0, second=0, microsecond=0)
                if limit < co:
                    limit = limit + datetime.timedelta(days=1)
                if now > limit:
                    return "ATRASADA"
            except Exception:
                pass
            return "EM_USO"
        return "DISPONÍVEL"

    df["status"] = df.apply(compute_status, axis=1)
    return df[["key_number","room_name","location","category","status","checkout_time","due_time","checkin_time"]].sort_values("key_number")

def list_transactions(start: Optional[datetime.datetime] = None,
                      end: Optional[datetime.datetime] = None) -> pd.DataFrame:
    c = conn()
    base_q = "SELECT * FROM transactions"
    params: List[str] = []
    where = []
    if start:
        where.append("datetime(checkout_time) >= datetime(?)")
        params.append(start.isoformat(timespec="seconds"))
    if end:
        where.append("datetime(COALESCE(checkin_time, checkout_time)) <= datetime(?)")
        params.append(end.isoformat(timespec="seconds"))
    if where:
        base_q += " WHERE " + " AND ".join(where)
    base_q += " ORDER BY checkout_time DESC"
    return pd.read_sql_query(base_q, c, params=params)

# -------------- Header & Sidebar -------------
st.title(APP_TITLE)

with st.sidebar:
    st.header("Acesso")
    typed_pass = st.text_input("Senha de admin", type="password", key="admin_pass",
                               help="Necessária para cadastrar/editar/exportar/gerar QRs.")
    is_admin = (ADMIN_PASS != "" and typed_pass == ADMIN_PASS)
    if ADMIN_PASS and is_admin:
        st.success("Admin autenticado.")
    elif ADMIN_PASS and not is_admin:
        st.caption("Modo operador/público: operação permitida; cadastros/relatórios/QRs completos só com senha.")
    else:
        st.info("Nenhuma senha configurada. Defina STREAMLIT_ADMIN_PASS em Secrets.")

with st.sidebar:
    st.header("Configuração de QR")
    if SECRET_BASE_URL:
        base_url = SECRET_BASE_URL
        st.caption(f"BASE_URL (secrets): {base_url}")
    else:
        base_url = st.text_input("Base URL (para QRs)", value="http://localhost:8501", key="qr_base_url",
                                 help="Defina BASE_URL em Secrets para fixar permanentemente.")

# Query params (?key=12&action=devolver&pid=<person_id>)
qp = st.query_params
qp_key = qp.get("key")
if isinstance(qp_key, list): qp_key = qp_key[0]
qp_action = qp.get("action")
if isinstance(qp_action, list): qp_action = qp_action[0]
if qp_action not in ("retirar", "devolver", "info"): qp_action = None
qp_pid = qp.get("pid")
if isinstance(qp_pid, list): qp_pid = qp_pid[0]

# -------------- Abas principais --------------
if is_admin:
    tab_op, tab_cad, tab_rep, tab_qr = st.tabs(["Operação", "Cadastros (Admin)", "Relatórios (Admin)", "QR Codes (Admin)"])
else:
    tab_op, = st.tabs(["Operação"])

# -------------- OPERAÇÃO ---------------------
with tab_op:
    st.subheader("Status das chaves")
    cats = ["Todas", "Sala", "Laboratório", "Secretaria"]
    sel_cat = st.selectbox("Filtrar por categoria", cats, index=0, key="op_cat")
    df_status = list_status()
    if sel_cat != "Todas":
        df_status = df_status[df_status["category"] == sel_cat]
    st.dataframe(df_status, use_container_width=True)
    num_atraso = (df_status["status"] == "ATRASADA").sum()
    if num_atraso:
        st.error(f"⚠️ {num_atraso} chave(s) ATRASADA(s).")

    st.markdown("---")
    st.subheader("Retirar / Devolver")

    modos = ["Retirar", "Devolver"]
    default_idx = 0 if (qp_action in (None, "retirar")) else 1
    modo = st.radio("Ação", modos, horizontal=True, index=default_idx, key="op_modo")

    default_key = int(qp_key) if (qp_key and str(qp_key).isdigit()) else None
    key_number = st.number_input("Nº da chave", min_value=1, step=1,
                                 value=default_key if default_key else 1, key="op_keynum")

    # Info do espaço
    df_spaces_all = list_spaces(active_only=False)
    room_info = df_spaces_all[df_spaces_all["key_number"] == int(key_number)]
    if not room_info.empty:
        rn = room_info.iloc[0]["room_name"]
        loc = room_info.iloc[0]["location"] or ""
        cat = room_info.iloc[0]["category"] or "Sala"
        st.caption(f"Sala/Lab: **{rn}**  •  Localização: {loc}  •  Categoria: {cat}")

    # Autorizados vigentes (se houver) — restringe a lista
    df_authorized_now = list_authorized_people_now(int(key_number))
    if df_authorized_now.empty:
        st.warning("⚠️ Não há autorização vigente para esta chave. (Admin pode cadastrar em Cadastros → Autorizações)")
        df_persons = list_persons(active_only=True)
    else:
        df_persons = df_authorized_now

    # Dados do responsável (com QR personalizado via pid)
    prefilled = None
    if qp_pid and not df_persons.empty and (df_persons["id"] == qp_pid).any():
        prow = df_persons[df_persons["id"] == qp_pid].iloc[0]
        prefilled = {"name": prow["name"], "id_code": prow["id_code"], "phone": prow["phone"]}

    st.markdown("**Dados do responsável**")
    use_registry = st.checkbox("Usar cadastro de responsável", value=True, key="op_use_registry")

    if prefilled:
        st.info(f"QR personalizado para **{prefilled['name']}**")
        taken_by_name = st.text_input("Nome de quem pegou", value=prefilled["name"], key="op_nome_pref", disabled=True)
        taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value=prefilled["id_code"], key="op_idcode_pref", disabled=True)
        taken_by_phone = st.text_input("Telefone", value=prefilled["phone"], key="op_phone_pref", disabled=True)
    elif use_registry and not df_persons.empty:
        sel_name = st.selectbox("Responsável (cadastro)", options=["-- selecione --"] + df_persons["name"].tolist(),
                                key="op_sel_person")
        if sel_name != "-- selecione --":
            rowp = df_persons[df_persons["name"] == sel_name].iloc[0]
            taken_by_name = st.text_input("Nome de quem pegou", value=rowp["name"], key="op_nome")
            taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value=rowp["id_code"], key="op_idcode")
            taken_by_phone= st.text_input("Telefone", value=rowp["phone"], key="op_phone")
        else:
            taken_by_name = st.text_input("Nome de quem pegou", value="", key="op_nome_blank")
            taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value="", key="op_idcode_blank")
            taken_by_phone= st.text_input("Telefone", value="", key="op_phone_blank")
    else:
        taken_by_name = st.text_input("Nome de quem pegou", value="", key="op_nome_manual")
        taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value="", key="op_idcode_manual")
        taken_by_phone= st.text_input("Telefone", value="", key="op_phone_manual")

    due_time = None
    if modo == "Retirar":
        due_choice = st.selectbox("Prazo de devolução", ["Hoje 12:00", "Hoje 18:00", "Outro", "Sem prazo"], key="op_due_choice")
        if due_choice == "Hoje 12:00":
            today = datetime.date.today()
            due_time = datetime.datetime.combine(today, datetime.time(12,0))
        elif due_choice == "Hoje 18:00":
            today = datetime.date.today()
            due_time = datetime.datetime.combine(today, datetime.time(18,0))
        elif due_choice == "Outro":
            due_time = st.datetime_input("Selecione data/hora prevista", key="op_due_dt")
        else:
            due_time = None

    if modo == "Retirar":
        st.caption("Assinatura – Entrega da chave")
        canvas_out = st_canvas(
            fill_color="rgba(0, 0, 0, 0)",
            stroke_width=2,
            stroke_color="#000000",
            background_color="#FFFFFF",
            height=180, width=500, drawing_mode="freedraw", key="sig_out"
        )
        if st.button("Confirmar retirada", key="btn_checkout"):
            sig_bytes = None
            if canvas_out.image_data is not None:
                try:
                    img = Image.fromarray((canvas_out.image_data).astype("uint8"))
                    buf = io.BytesIO(); img.save(buf, format="PNG"); sig_bytes = buf.getvalue()
                except Exception:
                    sig_bytes = None
            ok, msg = open_checkout(int(key_number), taken_by_name, taken_by_id, taken_by_phone, due_time, sig_bytes)
            if ok:
                st.success(f"Chave {int(key_number)} entregue. Protocolo: {msg}")
            else:
                st.error(msg)
    else:
        if not space_exists_and_active(int(key_number)):
            st.error(f"A chave {int(key_number)} não está cadastrada/ativa. Cadastre/ative em Cadastros → Espaços.")
        else:
            st.caption("Assinatura – Devolução da chave")
            canvas_in = st_canvas(
                fill_color="rgba(0, 0, 0, 0)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#FFFFFF",
                height=180, width=500, drawing_mode="freedraw", key="sig_in"
            )
            if st.button("Confirmar devolução", key="btn_checkin"):
                sig_bytes = None
                if canvas_in.image_data is not None:
                    try:
                        img = Image.fromarray((canvas_in.image_data).astype("uint8"))
                        buf = io.BytesIO(); img.save(buf, format="PNG"); sig_bytes = buf.getvalue()
                    except Exception:
                        sig_bytes = None
                ok, msg = do_checkin(int(key_number), sig_bytes)
                if ok:
                    st.success(f"Chave {int(key_number)} devolvida. Protocolo: {msg}")
                else:
                    st.error(msg)

# -------------- CADASTROS (ADMIN) -----------
if is_admin:
    with tab_cad:
        st.subheader("Espaços (Chaves/Salas)")
        df_sp = list_spaces(active_only=False)
        st.dataframe(df_sp, use_container_width=True)

        st.markdown("**Adicionar/Atualizar espaço**")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            sp_key = st.number_input("Nº da chave", min_value=1, step=1, key="space_key_add")
        with c2:
            sp_name = st.text_input("Nome da Sala/Lab", key="space_name_add")
        with c3:
            sp_loc = st.text_input("Localização (opcional)", key="space_loc_add")
        with c4:
            sp_cat = st.selectbox("Categoria", ["Sala", "Laboratório", "Secretaria"], key="space_cat_add")
        if st.button("Salvar/Atualizar espaço", key="space_save"):
            if sp_name.strip():
                add_space(int(sp_key), sp_name.strip(), sp_loc.strip(), sp_cat)
                st.success("Espaço salvo/atualizado.")
            else:
                st.error("Informe o nome da Sala/Lab.")

        st.markdown("---")
        des_key = st.number_input("Ativar/Desativar - Nº da chave", min_value=1, step=1, key="space_key_status")
        des_active = st.selectbox("Status", ["Ativar", "Desativar"], index=0, key="space_status_select")
        if st.button("Aplicar status", key="space_status_apply"):
            row = df_sp[df_sp["key_number"] == int(des_key)]
            if row.empty:
                st.error("Chave não encontrada.")
            else:
                update_space(int(des_key),
                             row.iloc[0]["room_name"],
                             row.iloc[0]["location"] or "",
                             1 if des_active == "Ativar" else 0,
                             row.iloc[0].get("category", "Sala"))
                st.success("Status atualizado.")

        st.markdown("---")
        st.caption("Atalho: criar chaves 1..50 (categoria 'Sala').")
        if st.button("Gerar 50 chaves padrão", key="space_generate_50"):
            for k in range(1, 51):
                add_space(k, f"Sala/Lab {k}", "", "Sala")
            st.success("Criadas/atualizadas as chaves 1..50.")

        st.markdown("___")
        st.subheader("Responsáveis")
        df_pe = list_persons(active_only=False)
        st.dataframe(df_pe, use_container_width=True)

        st.markdown("**Adicionar responsável**")
        p1, p2, p3 = st.columns(3)
        with p1:
            pn = st.text_input("Nome", key="add_nome")
        with p2:
            pidc = st.text_input("SIAPE / Matrícula", key="add_idcode")
        with p3:
            pph = st.text_input("Telefone", key="add_phone")
        if st.button("Salvar responsável", key="add_person_btn"):
            if pn.strip():
                add_person(pn.strip(), pidc.strip(), pph.strip())
                st.success("Responsável adicionado.")
            else:
                st.error("Informe o nome.")

        st.markdown("**Editar responsável**")
        if not df_pe.empty:
            sel_pid = st.selectbox("Selecione", options=df_pe["id"].tolist(), key="edit_select")
            prow = df_pe[df_pe["id"] == sel_pid].iloc[0]
            en = st.text_input("Nome", value=prow["name"], key="edit_nome")
            eidc = st.text_input("SIAPE / Matrícula", value=prow["id_code"], key="edit_idcode")
            eph = st.text_input("Telefone", value=prow["phone"], key="edit_phone")
            est = st.selectbox("Status", ["Ativo","Inativo"],
                               index=0 if prow["is_active"]==1 else 1, key="edit_status")
            if st.button("Atualizar responsável", key="edit_person_btn"):
                update_person(sel_pid, en.strip(), eidc.strip(), eph.strip(), 1 if est=="Ativo" else 0)
                st.success("Responsável atualizado.")

        st.markdown("___")
        st.subheader("Autorizações por espaço")
        df_sp_act = list_spaces(active_only=True)
        if df_sp_act.empty:
            st.info("Cadastre espaços ativos para criar autorizações.")
        else:
            key_sel = st.selectbox("Chave", options=df_sp_act["key_number"].tolist(), key="auth_key_sel")
            memo = st.text_input("Nº do memorando/circular", key="auth_memo")
            col_af, col_at = st.columns(2)
            with col_af:
                vf = st.date_input("Válido de (opcional)", key="auth_from")
            with col_at:
                vt = st.date_input("Válido até (opcional)", key="auth_to")
            if st.button("Criar autorização", key="auth_create"):
                aid = add_authorization(int(key_sel), memo.strip(), vf if vf else None, vt if vt else None)
                st.success(f"Autorização criada: {aid}")

            st.markdown("**Vincular pessoas à autorização**")
            df_auths = list_authorizations(int(key_sel))
            if df_auths.empty:
                st.info("Nenhuma autorização criada para esta chave.")
            else:
                sel_auth = st.selectbox("Selecione a autorização", options=df_auths["id"].tolist(), key="auth_sel")
                # pessoas ativas
                dfp = list_persons(active_only=True)
                if not dfp.empty:
                    sel_people = st.multiselect("Adicionar pessoas (ativas)", options=dfp["name"].tolist(), key="auth_people_sel")
                    if st.button("Adicionar à autorização", key="auth_people_add"):
                        for nm in sel_people:
                            pid = dfp[dfp["name"]==nm].iloc[0]["id"]
                            add_person_to_authorization(sel_auth, pid)
                        st.success("Pessoas adicionadas.")
                # lista vinculados
                c = conn()
                df_link = pd.read_sql_query("""
                    SELECT p.name, p.id_code, p.phone FROM persons p
                    JOIN authorization_people ap ON ap.person_id = p.id
                    WHERE ap.authorization_id=?
                """, c, params=[sel_auth])
                st.write("Vinculados:")
                st.dataframe(df_link, use_container_width=True)

# -------------- RELATÓRIOS (ADMIN) ----------
if is_admin:
    with tab_rep:
        st.subheader("Movimentações")
        colr1, colr2 = st.columns(2)
        with colr1:
            dt_start = st.date_input("Início (opcional)", key="rep_start")
        with colr2:
            dt_end = st.date_input("Fim (opcional)", key="rep_end")
        start_dt = datetime.datetime.combine(dt_start, datetime.time.min) if dt_start else None
        end_dt   = datetime.datetime.combine(dt_end, datetime.time.max) if dt_end else None

        df_tx = list_transactions(start_dt, end_dt)
        st.dataframe(df_tx, use_container_width=True)

        total = len(df_tx)
        em_uso = sum((pd.isna(df_tx["checkin_time"])))
        atrasadas = 0
        for _, r in df_tx.iterrows():
            if pd.isna(r["checkin_time"]):
                if pd.notna(r["due_time"]):
                    try:
                        due = datetime.datetime.fromisoformat(str(r["due_time"]))
                        if datetime.datetime.now() > due:
                            atrasadas += 1
                    except Exception:
                        pass
                else:
                    try:
                        co = datetime.datetime.fromisoformat(str(r["checkout_time"]))
                        limit = co.replace(hour=CUTOFF_HOUR_FOR_OVERDUE, minute=0, second=0, microsecond=0)
                        if limit < co:
                            limit = limit + datetime.timedelta(days=1)
                        if datetime.datetime.now() > limit:
                            atrasadas += 1
                    except Exception:
                        pass
        m1, m2, m3 = st.columns(3)
        m1.metric("Movimentações", total)
        m2.metric("Em uso (abertas)", em_uso)
        m3.metric("Atrasadas (abertas)", atrasadas)

        csv = df_tx.to_csv(index=False).encode("utf-8")
        st.download_button("Baixar CSV", data=csv, file_name="movimentacoes.csv", key="rep_csv_btn")

# -------------- QR CODES (ADMIN) ------------
if is_admin:
    with tab_qr:
        st.subheader("QR Codes por chave")
        if not base_url:
            st.error("Defina a BASE_URL (em Secrets ou na sidebar) para gerar QRs públicos.")
        df_sp_act = list_spaces(active_only=True)
        if df_sp_act.empty:
            st.info("Nenhuma chave ativa cadastrada.")
        else:
            ids = st.multiselect("Selecione as chaves", options=df_sp_act["key_number"].tolist(),
                                 default=df_sp_act["key_number"].tolist()[:12], key="qr_ids")
            cols = st.number_input("Cartões por linha (sug.: 4)", min_value=1, max_value=6, value=4, key="qr_cols")
            modo_qr = st.selectbox("Ação padrão ao abrir o QR", ["Somente info", "Retirar", "Devolver"], key="qr_action")
            action_map = {"Somente info":"info", "Retirar":"retirar", "Devolver":"devolver"}
            action = action_map[modo_qr]

            images_for_zip = []
            if ids:
                rows = (len(ids) + cols - 1) // cols
                for r in range(rows):
                    cset = st.columns(int(cols))
                    for c, keyn in enumerate(ids[r*int(cols):(r+1)*int(cols)]):
                        with cset[c]:
                            room = df_sp_act[df_sp_act["key_number"] == keyn].iloc[0]["room_name"]
                            url = build_url(base_url, {"key": keyn, "action": action})
                            img = make_qr(url)
                            st.image(img, use_container_width=True)
                            st.caption(f"Chave {keyn} — {room}")
                            st.caption(url)
                            images_for_zip.append((f"chave_{keyn}.png", to_png_bytes(img)))

                if images_for_zip:
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for fname, data in images_for_zip:
                            zf.writestr(fname, data)
                    buf.seek(0)
                    st.download_button("Baixar todas em ZIP", data=buf.read(), file_name="qrcodes_chaves.zip", key="qr_zip_btn")

        st.markdown("---")
        st.subheader("QR personalizado (pessoa específica)")
        dfp_all = list_persons(active_only=True)
        if dfp_all.empty or df_sp_act.empty:
            st.info("Cadastre pessoas e espaços para gerar QR personalizado.")
        else:
            sel_key_personal = st.selectbox("Chave", options=df_sp_act["key_number"].tolist(), key="qr_pers_key")
            sel_person = st.selectbox("Responsável", options=dfp_all["name"].tolist(), key="qr_pers_person")
            pid_val = dfp_all[dfp_all["name"] == sel_person].iloc[0]["id"]
            url_p = build_url(base_url, {"key": sel_key_personal, "action": "retirar", "pid": pid_val})
            img_p = make_qr(url_p)
            st.image(img_p, use_container_width=False)
            st.caption(url_p)
            st.download_button("Baixar QR (PNG)", data=to_png_bytes(img_p),
                               file_name=f"qr_key{sel_key_personal}_{pid_val[:8]}.png", key="qr_pers_dl")
