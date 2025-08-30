# -*- coding: utf-8 -*-
"""
Created on Sat Aug 30 14:39:09 2025

@author: Suporte
"""

# ==========================================
# Guarita - Controle de Chaves (Completo)
# ==========================================
import os, io, uuid, sqlite3, datetime, zipfile
from typing import Optional, Tuple, List
import pandas as pd
import streamlit as st
from PIL import Image
from streamlit_drawable_canvas import st_canvas
import qrcode

# -------------- Configurações --------------
st.set_page_config(page_title="Guarita - Controle de Chaves", layout="wide")
APP_TITLE = "Guarita – Controle de Chaves"

ADMIN_PASS = st.secrets.get("STREAMLIT_ADMIN_PASS", os.getenv("STREAMLIT_ADMIN_PASS", ""))
SECRET_BASE_URL = st.secrets.get("BASE_URL", os.getenv("BASE_URL", "")).strip()
DB_PATH = os.getenv("DB_PATH", "keys.db")

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
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{base}/?{query}" if query else f"{base}/"

# -------------- Banco de Dados -------------
def conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""PRAGMA foreign_keys = ON;""")
    c.execute("""
      CREATE TABLE IF NOT EXISTS spaces(
        key_number INTEGER PRIMARY KEY,
        room_name  TEXT NOT NULL,
        location   TEXT,
        is_active  INTEGER DEFAULT 1
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS persons(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        id_code TEXT,      -- SIAPE ou matrícula
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
    return c

# ----- CRUD helpers: Spaces -----
def add_space(key_number: int, room_name: str, location: str = ""):
    c = conn()
    with c:
        c.execute("INSERT OR REPLACE INTO spaces(key_number,room_name,location,is_active) VALUES(?,?,?,1)",
                  (key_number, room_name, location))

def list_spaces(active_only=True):
    c = conn()
    if active_only:
        return pd.read_sql_query("SELECT * FROM spaces WHERE is_active=1 ORDER BY key_number", c)
    return pd.read_sql_query("SELECT * FROM spaces ORDER BY key_number", c)

def update_space(key_number: int, room_name: str, location: str, is_active: int):
    c = conn()
    with c:
        c.execute("UPDATE spaces SET room_name=?, location=?, is_active=? WHERE key_number=?",
                  (room_name, location, int(is_active), key_number))

# ----- CRUD helpers: Persons -----
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

# ----- Transactions / Operação -----
def has_open_checkout(key_number: int) -> bool:
    c = conn()
    cur = c.cursor()
    cur.execute("""SELECT 1 FROM transactions 
                   WHERE key_number=? AND checkin_time IS NULL
                   LIMIT 1""", (key_number,))
    return cur.fetchone() is not None

def open_checkout(key_number: int, name: str, id_code: str, phone: str,
                  due_time: Optional[datetime.datetime], signature_png: Optional[bytes]) -> Tuple[bool, str]:
    if has_open_checkout(key_number):
        return False, "Esta chave já está EM USO. Faça a devolução antes de nova retirada."
    c = conn()
    tid = str(uuid.uuid4())
    with c:
        c.execute("""INSERT INTO transactions
                     (id,key_number,taken_by_name,taken_by_id,taken_phone,checkout_time,due_time,checkin_time,status,signature_out,signature_in)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (tid, key_number, name.strip(), id_code.strip(), phone.strip(),
                   now_iso(),
                   due_time.isoformat(timespec="seconds") if due_time else None,
                   None, "EM_USO", signature_png, None))
    return True, tid

def do_checkin(key_number: int, signature_png: Optional[bytes]) -> Tuple[bool, str]:
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
    df_space = list_spaces(active_only=True)
    df_tx = pd.read_sql_query("""
        SELECT t.key_number, t.checkout_time, t.due_time, t.checkin_time, t.status AS last_status
        FROM transactions t
        INNER JOIN (
          SELECT key_number, MAX(checkout_time) AS max_co FROM transactions GROUP BY key_number
        ) m ON t.key_number=m.key_number AND t.checkout_time=m.max_co
    """, c)
    df = df_space.merge(df_tx, on="key_number", how="left")
    # Computa status atual
    def compute_status(row):
        if pd.isna(row["checkout_time"]):
            return "DISPONÍVEL"
        if pd.isna(row["checkin_time"]):
            # em uso; checa atraso
            if pd.notna(row["due_time"]):
                try:
                    due = datetime.datetime.fromisoformat(str(row["due_time"]))
                    if datetime.datetime.now() > due:
                        return "ATRASADA"
                except Exception:
                    pass
            return "EM_USO"
        return "DISPONÍVEL"
    df["status"] = df.apply(compute_status, axis=1)
    return df[["key_number","room_name","location","status","checkout_time","due_time","checkin_time"]].sort_values("key_number")

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

# Autenticação admin
with st.sidebar:
    st.header("Acesso")
    typed_pass = st.text_input("Senha de admin", type="password",
                               help="Necessária para cadastrar/editar/exportar/gerar QRs.")
    is_admin = (ADMIN_PASS != "" and typed_pass == ADMIN_PASS)
    if ADMIN_PASS and is_admin:
        st.success("Admin autenticado.")
    elif ADMIN_PASS and not is_admin:
        st.caption("Modo operador/público: operação permitida; cadastros/relatórios/QRs completos só com senha.")
    else:
        st.info("Nenhuma senha configurada (atenção em produção). Defina STREAMLIT_ADMIN_PASS em Secrets.")

# BASE_URL para gerar QR
with st.sidebar:
    st.header("Configuração de QR")
    if SECRET_BASE_URL:
        base_url = SECRET_BASE_URL
        st.caption(f"BASE_URL (secrets): {base_url}")
    else:
        base_url = st.text_input("Base URL (para QRs)", value="http://localhost:8501",
                                 help="Defina BASE_URL em Secrets para fixar permanentemente.")

# Interpreta query params (ex.: ?key=12&action=devolver)
qp = st.query_params
qp_key = qp.get("key")
if isinstance(qp_key, list): qp_key = qp_key[0]
qp_action = qp.get("action")
if isinstance(qp_action, list): qp_action = qp_action[0]
if qp_action not in ("retirar", "devolver", "info"):
    qp_action = None

# -------------- Abas principais --------------
if is_admin:
    tab_op, tab_cad, tab_rep, tab_qr = st.tabs(["Operação", "Cadastros (Admin)", "Relatórios (Admin)", "QR Codes (Admin)"])
else:
    tab_op, = st.tabs(["Operação"])

# -------------- OPERAÇÃO ---------------------
with tab_op:
    st.subheader("Status das chaves")
    df_status = list_status()
    st.dataframe(df_status, use_container_width=True)

    st.markdown("---")
    st.subheader("Retirar / Devolver")

    # Define modo conforme query param ou escolha
    modos = ["Retirar", "Devolver"]
    default_idx = 0 if (qp_action in (None, "retirar")) else 1
    modo = st.radio("Ação", modos, horizontal=True, index=default_idx)

    # Número da chave (prefill por query param)
    default_key = int(qp_key) if (qp_key and str(qp_key).isdigit()) else None
    key_number = st.number_input("Nº da chave", min_value=1, step=1, value=default_key if default_key else 1)

    # Busca dados da sala para exibir
    df_spaces_all = list_spaces(active_only=False)
    room_info = df_spaces_all[df_spaces_all["key_number"] == int(key_number)]
    if not room_info.empty:
        rn = room_info.iloc[0]["room_name"]
        loc = room_info.iloc[0]["location"] or ""
        st.caption(f"Sala/Lab: **{rn}**  •  Localização: {loc}")

    # Dados do responsável: pode usar cadastro ou preencher manualmente
    st.markdown("**Dados do responsável**")
    df_persons = list_persons(active_only=True)
    use_registry = st.checkbox("Usar cadastro de responsável", value=True)

    if use_registry and not df_persons.empty:
        sel_name = st.selectbox("Responsável (cadastro)", options=["-- selecione --"] + df_persons["name"].tolist())
        if sel_name != "-- selecione --":
            rowp = df_persons[df_persons["name"] == sel_name].iloc[0]
            taken_by_name = st.text_input("Nome de quem pegou", value=rowp["name"])
            taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value=rowp["id_code"])
            taken_phone   = st.text_input("Telefone", value=rowp["phone"])
        else:
            taken_by_name = st.text_input("Nome de quem pegou", value="")
            taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value="")
            taken_phone   = st.text_input("Telefone", value="")
    else:
        taken_by_name = st.text_input("Nome de quem pegou", value="")
        taken_by_id   = st.text_input("Matrícula SIAPE / ID estudante", value="")
        taken_phone   = st.text_input("Telefone", value="")

    # Prazos padrão
    due_choice = None
    due_time = None
    if modo == "Retirar":
        due_choice = st.selectbox("Prazo de devolução", ["Hoje 12:00", "Hoje 18:00", "Outro", "Sem prazo"])
        if due_choice == "Hoje 12:00":
            today = datetime.date.today()
            due_time = datetime.datetime.combine(today, datetime.time(12,0))
        elif due_choice == "Hoje 18:00":
            today = datetime.date.today()
            due_time = datetime.datetime.combine(today, datetime.time(18,0))
        elif due_choice == "Outro":
            due_time = st.datetime_input("Selecione data/hora prevista")
        else:
            due_time = None

    # Assinaturas
    col1, col2 = st.columns(2)
    if modo == "Retirar":
        with col1:
            st.caption("Assinatura – Entrega da chave")
            canvas_out = st_canvas(
                fill_color="rgba(0, 0, 0, 0)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#FFFFFF",
                height=180, width=500, drawing_mode="freedraw", key="sig_out"
            )
    else:
        with col1:
            st.caption("Assinatura – Devolução da chave")
            canvas_in = st_canvas(
                fill_color="rgba(0, 0, 0, 0)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#FFFFFF",
                height=180, width=500, drawing_mode="freedraw", key="sig_in"
            )

    # Botões de ação (restringe confirmação se não admin)
    st.markdown("")
    if modo == "Retirar":
        can_submit = True  # operador pode registrar operação
        btn = st.button("Confirmar retirada")
        if btn:
            if not taken_by_name.strip():
                st.error("Informe o nome de quem pegou.")
            else:
                sig_bytes = None
                if 'canvas_out' in locals() and canvas_out.image_data is not None:
                    try:
                        img = Image.fromarray((canvas_out.image_data).astype("uint8"))
                        buf = io.BytesIO(); img.save(buf, format="PNG"); sig_bytes = buf.getvalue()
                    except Exception:
                        sig_bytes = None
                ok, msg = open_checkout(int(key_number), taken_by_name, taken_by_id, taken_phone, due_time, sig_bytes)
                if ok:
                    st.success(f"Chave {int(key_number)} entregue. Protocolo: {msg}")
                else:
                    st.error(msg)

    else:  # Devolver
        btn = st.button("Confirmar devolução")
        if btn:
            sig_bytes = None
            if 'canvas_in' in locals() and canvas_in.image_data is not None:
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
        c1, c2, c3 = st.columns(3)
        with c1:
            sp_key = st.number_input("Nº da chave", min_value=1, step=1)
        with c2:
            sp_name = st.text_input("Nome da Sala/Lab")
        with c3:
            sp_loc = st.text_input("Localização (opcional)")
        c4, c5 = st.columns(2)
        with c4:
            if st.button("Salvar/Atualizar espaço"):
                if sp_name.strip():
                    add_space(int(sp_key), sp_name.strip(), sp_loc.strip())
                    st.success("Espaço salvo/atualizado.")
                else:
                    st.error("Informe o nome da Sala/Lab.")
        with c5:
            des_key = st.number_input("Ativar/Desativar - Nº da chave", min_value=1, step=1, key="des_key")
            des_active = st.selectbox("Status", ["Ativar", "Desativar"], index=0)
            if st.button("Aplicar status"):
                row = df_sp[df_sp["key_number"] == int(des_key)]
                if row.empty:
                    st.error("Chave não encontrada.")
                else:
                    update_space(int(des_key),
                                 row.iloc[0]["room_name"],
                                 row.iloc[0]["location"] or "",
                                 1 if des_active == "Ativar" else 0)
                    st.success("Status atualizado.")

        st.markdown("---")
        st.caption("Atalho: criar chaves 1..50 (apenas nome genérico).")
        if st.button("Gerar 50 chaves padrão"):
            for k in range(1, 51):
                add_space(k, f"Sala/Lab {k}", "")
            st.success("Criadas/atualizadas as chaves 1..50.")

        st.markdown("___")
        st.subheader("Responsáveis")
        df_pe = list_persons(active_only=False)
        st.dataframe(df_pe, use_container_width=True)

        st.markdown("**Adicionar responsável**")
        p1, p2, p3 = st.columns(3)
        with p1:
            pn = st.text_input("Nome")
        with p2:
            pidc = st.text_input("SIAPE / Matrícula")
        with p3:
            pph = st.text_input("Telefone")
        if st.button("Salvar responsável"):
            if pn.strip():
                add_person(pn.strip(), pidc.strip(), pph.strip())
                st.success("Responsável adicionado.")
            else:
                st.error("Informe o nome.")

        st.markdown("**Editar responsável**")
        if not df_pe.empty:
            sel_pid = st.selectbox("Selecione", options=df_pe["id"].tolist())
            prow = df_pe[df_pe["id"] == sel_pid].iloc[0]
            en = st.text_input("Nome", value=prow["name"])
            eidc = st.text_input("SIAPE / Matrícula", value=prow["id_code"])
            eph = st.text_input("Telefone", value=prow["phone"])
            est = st.selectbox("Status", ["Ativo","Inativo"], index=0 if prow["is_active"]==1 else 1)
            if st.button("Atualizar responsável"):
                update_person(sel_pid, en.strip(), eidc.strip(), eph.strip(), 1 if est=="Ativo" else 0)
                st.success("Responsável atualizado.")

# -------------- RELATÓRIOS (ADMIN) ----------
if is_admin:
    with tab_rep:
        st.subheader("Movimentações")
        colr1, colr2 = st.columns(2)
        with colr1:
            dt_start = st.date_input("Início (opcional)")
        with colr2:
            dt_end = st.date_input("Fim (opcional)")
        start_dt = datetime.datetime.combine(dt_start, datetime.time.min) if dt_start else None
        end_dt   = datetime.datetime.combine(dt_end, datetime.time.max) if dt_end else None

        df_tx = list_transactions(start_dt, end_dt)
        st.dataframe(df_tx, use_container_width=True)

        # Métricas
        total = len(df_tx)
        em_uso = sum((pd.isna(df_tx["checkin_time"])))
        atrasadas = 0
        for _, r in df_tx.iterrows():
            if pd.isna(r["checkin_time"]) and pd.notna(r["due_time"]):
                try:
                    due = datetime.datetime.fromisoformat(str(r["due_time"]))
                    if datetime.datetime.now() > due:
                        atrasadas += 1
                except Exception:
                    pass
        devolvidas = total - em_uso
        m1, m2, m3 = st.columns(3)
        m1.metric("Movimentações", total)
        m2.metric("Em uso (abertas)", em_uso)
        m3.metric("Atrasadas (abertas)", atrasadas)

        # Exportar CSV
        csv = df_tx.to_csv(index=False).encode("utf-8")
        st.download_button("Baixar CSV", data=csv, file_name="movimentacoes.csv")

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
                                 default=df_sp_act["key_number"].tolist()[:12])
            cols = st.number_input("Cartões por linha (sug.: 4)", min_value=1, max_value=6, value=4)
            modo_qr = st.selectbox("Ação padrão ao abrir o QR", ["Somente info", "Retirar", "Devolver"])
            action_map = {"Somente info":"info", "Retirar":"retirar", "Devolver":"devolver"}
            action = action_map[modo_qr]

            # Geração em grade + opcional ZIP
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

                # Download em ZIP
                if images_for_zip:
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for fname, data in images_for_zip:
                            zf.writestr(fname, data)
                    buf.seek(0)
                    st.download_button("Baixar todas em ZIP", data=buf.read(), file_name="qrcodes_chaves.zip")
