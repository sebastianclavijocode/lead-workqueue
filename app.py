import datetime as dt
import json
import random
import smtplib
import ssl
from email.mime.text import MIMEText

import pandas as pd
import streamlit as st

from db import (
    init_db, get_session, hash_pw, NO_CONTESTA_LIMIT,
    User, Campaign, Lead, Tipificacion, Gestion, AuditLog,
)

st.set_page_config(page_title="Work Queue - Leads Fríos", layout="wide")
init_db()

PRIMARY = "#f57c00"

st.markdown(f"""
<style>
.stButton>button {{ border-radius: 8px; font-weight: 600; }}
.metric-card {{ background:#1e1e1e; padding:16px; border-radius:12px; border:1px solid #333; }}
h1, h2, h3 {{ color:{PRIMARY}; }}
</style>
""", unsafe_allow_html=True)


def log(session, user_id, action, lead_id=None, detail=None):
    session.add(AuditLog(user_id=user_id, action=action, lead_id=lead_id, detail=detail))
    session.commit()


# ---------------------------------------------------------------- EMAIL (recordatorios "Llamar después")
def get_smtp_config():
    """Lee la configuración SMTP desde .streamlit/secrets.toml. Devuelve None si no está configurada."""
    try:
        cfg = st.secrets.get("smtp")
        if cfg and cfg.get("host") and cfg.get("user") and cfg.get("password"):
            return cfg
    except Exception:
        return None
    return None


def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    cfg = get_smtp_config()
    if not cfg:
        return False, "SMTP no configurado (.streamlit/secrets.toml)."
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = cfg.get("from", cfg["user"])
        msg["To"] = to_email
        context = ssl.create_default_context()
        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587))) as server:
            server.starttls(context=context)
            server.login(cfg["user"], cfg["password"])
            server.sendmail(msg["From"], [to_email], msg.as_string())
        return True, "Enviado."
    except Exception as e:
        return False, str(e)


def send_due_reminders(session, user):
    """Revisa, cada vez que el asesor abre la app, si hay seguimientos ('Llamar después')
    cuya fecha/hora ya se cumplió y envía el correo pendiente. Es una verificación 'al vuelo',
    no un scheduler real: solo dispara mientras la app está abierta. Para producción se
    recomienda un job (cron/APScheduler) que corra de forma independiente."""
    if not user.email:
        return
    due = (session.query(Lead)
           .filter(Lead.assigned_to == user.id, Lead.reminder_sent == False,  # noqa: E712
                   Lead.next_follow_up.isnot(None),
                   Lead.next_follow_up <= dt.datetime.utcnow())
           .all())
    for lead in due:
        ok, _ = send_email(
            user.email,
            f"Recordatorio de seguimiento — {lead.name or 'Lead #' + str(lead.id)}",
            f"Es hora de contactar de nuevo a {lead.name} ({lead.phone}).\n"
            f"Programado para: {lead.next_follow_up:%Y-%m-%d %H:%M}\n"
            f"Carrera: {lead.carrera or '-'} | Dolor: {lead.dolor or '-'}",
        )
        lead.reminder_sent = ok  # si falla el envío, se reintenta en el siguiente check
    if due:
        session.commit()


def randomize_position(session, user_id, exclude_lead_id):
    """Calcula un sort_key que inserta el lead en una posición aleatoria dentro
    de la cola de pendientes del asesor (usado para 'No contesta')."""
    others = (session.query(Lead.sort_key)
              .filter(Lead.assigned_to == user_id, Lead.status == "pending",
                      Lead.id != exclude_lead_id)
              .order_by(Lead.sort_key.asc()).all())
    keys = [o[0] if o[0] is not None else 0.0 for o in others]
    if not keys:
        return dt.datetime.utcnow().timestamp()
    idx = random.randint(0, len(keys))
    if idx == 0:
        return keys[0] - random.uniform(0.5, 2.0)
    if idx == len(keys):
        return keys[-1] + random.uniform(0.5, 2.0)
    return (keys[idx - 1] + keys[idx]) / 2


# ---------------------------------------------------------------- LOGIN
def login_view():
    st.title("🔒 Work Queue — Ingreso")
    with st.form("login"):
        u = st.text_input("Usuario")
        p = st.text_input("Contraseña", type="password")
        ok = st.form_submit_button("Ingresar")
    if ok:
        session = get_session()
        user = session.query(User).filter_by(username=u, active=True).first()
        if user and user.password_hash == hash_pw(p):
            st.session_state["user_id"] = user.id
            st.session_state["role"] = user.role
            log(session, user.id, "LOGIN")
            session.close()
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")
        session.close()
    st.caption("Demo: admin/admin123 · supervisor/super123 · asesor1/asesor123")


# ---------------------------------------------------------------- ASESOR (WORK QUEUE)
def asesor_view(user):
    session = get_session()
    st.title(f"👋 Hola, {user.name}")

    send_due_reminders(session, user)

    with st.expander("✉️ Mi correo para recordatorios de seguimiento"):
        email_input = st.text_input("Correo", value=user.email or "", key="email_input")
        if st.button("Guardar correo"):
            user.email = email_input.strip()
            session.commit()
            st.success("Correo actualizado.")

    pending_count = session.query(Lead).filter_by(assigned_to=user.id, status="pending").count()
    in_progress_count = session.query(Lead).filter_by(assigned_to=user.id, status="in_progress").count()
    done_today = session.query(Gestion).filter(
        Gestion.user_id == user.id,
        Gestion.closed_lead == True,  # noqa: E712 — solo cierres reales, no intentos como "No contesta"
        Gestion.created_at >= dt.datetime.combine(dt.date.today(), dt.time.min),
    ).count()
    c1, c2 = st.columns(2)
    c1.markdown(f'<div class="metric-card"><h3>Pendientes</h3><h1>{pending_count + in_progress_count}</h1></div>',
                unsafe_allow_html=True)
    c2.markdown(f'<div class="metric-card"><h3>Gestionados hoy</h3><h1>{done_today}</h1></div>',
                unsafe_allow_html=True)
    st.divider()

    # El lead "en progreso" (ya abierto) siempre tiene prioridad para que la pantalla
    # no salte al siguiente lead solo por cambiar la tipificación sin guardar.
    lead = (session.query(Lead)
            .filter_by(assigned_to=user.id, status="in_progress")
            .order_by(Lead.created_at.asc())
            .first())
    just_opened = False
    if not lead:
        lead = (session.query(Lead)
                .filter_by(assigned_to=user.id, status="pending")
                .order_by(Lead.sort_key.asc().nullslast(), Lead.created_at.asc())
                .first())
        if lead:
            lead.status = "in_progress"
            session.commit()
            just_opened = True

    if not lead:
        st.success("🎉 No tienes leads pendientes en este momento.")
        session.close()
        return

    if just_opened:
        log(session, user.id, "LEAD_OPENED", lead.id)

    extra = json.loads(lead.extra_info or "{}")

    st.subheader("📋 Lead actual")
    col1, col2 = st.columns([2, 1])
    with col1:
        st.markdown(f"**Nombre:** {lead.name or '—'}")
        st.markdown(f"**Teléfono:** {lead.phone or '—'}")
        st.markdown(f"**Carrera:** {lead.carrera or '—'}")
        st.markdown(f"**Años de experiencia:** {lead.anos_experiencia or '—'}")
        st.markdown(f"**Dolor:** {lead.dolor or '—'}")
        st.markdown(f"**Monto económico:** {lead.monto_economico or '—'}")
        if lead.no_contesta_count:
            st.caption(f"Intentos de 'No contesta': {lead.no_contesta_count}/{NO_CONTESTA_LIMIT}")
        if extra:
            with st.expander("Información adicional"):
                for k, v in extra.items():
                    st.markdown(f"- **{k}:** {v}")
    with col2:
        with st.expander(f"Historial ({len(lead.gestiones)})", expanded=False):
            if not lead.gestiones:
                st.caption("Sin gestiones previas.")
            for g in lead.gestiones:
                st.caption(f"{g.created_at:%Y-%m-%d %H:%M} · {g.tipificacion.name} — {g.notes or ''}")

    st.divider()
    st.subheader("✅ Tipificar gestión")

    tips = session.query(Tipificacion).all()
    tip_names = [t.name for t in tips]
    chosen_name = st.selectbox("Tipificación", tip_names, key=f"tip_{lead.id}")
    chosen = next(t for t in tips if t.name == chosen_name)

    extra_values = {}
    req_fields = chosen.fields()
    if req_fields:
        st.caption("Esta tipificación requiere información adicional (se abre el calendario para elegir fecha/hora):")
        cols = st.columns(len(req_fields))
        for i, f in enumerate(req_fields):
            with cols[i]:
                if f == "fecha":
                    extra_values[f] = str(st.date_input("Fecha", key=f"fecha_{lead.id}"))
                elif f == "hora":
                    extra_values[f] = str(st.time_input("Hora", key=f"hora_{lead.id}"))
                else:
                    extra_values[f] = st.text_input(f.capitalize(), key=f"{f}_{lead.id}")

    notes = st.text_area("Notas", key=f"notes_{lead.id}")

    if st.button("💾 Guardar y siguiente", type="primary", use_container_width=True):
        missing = [f for f in req_fields if not str(extra_values.get(f, "")).strip()]
        if chosen.name == "Llamar después" and not user.email:
            st.warning("Guarda primero tu correo arriba para poder recibir el recordatorio por email.")
        if missing:
            st.error(f"Completa los campos obligatorios: {', '.join(missing)}")
        else:
            gestion = Gestion(
                lead_id=lead.id, user_id=user.id, tipificacion_id=chosen.id,
                notes=notes, extra_data=json.dumps(extra_values),
            )
            session.add(gestion)

            if chosen.name == "No contesta":
                lead.no_contesta_count += 1
                if lead.no_contesta_count >= NO_CONTESTA_LIMIT:
                    lead.status = "done"
                else:
                    lead.status = "pending"
                    lead.sort_key = randomize_position(session, user.id, lead.id)

            elif chosen.name == "Llamar después" and extra_values.get("fecha"):
                lead.status = "pending"
                fecha = extra_values["fecha"]
                hora = extra_values.get("hora") or "00:00:00"
                try:
                    lead.next_follow_up = dt.datetime.fromisoformat(f"{fecha}T{hora}")
                except ValueError:
                    lead.next_follow_up = dt.datetime.fromisoformat(fecha)
                lead.reminder_sent = False
                lead.sort_key = randomize_position(session, user.id, lead.id)
                if user.email:
                    send_email(
                        user.email,
                        f"Seguimiento programado — {lead.name or 'Lead #' + str(lead.id)}",
                        f"Se programó un recordatorio para contactar a {lead.name} ({lead.phone}) "
                        f"el {lead.next_follow_up:%Y-%m-%d %H:%M}.",
                    )

            elif not chosen.is_final:
                lead.status = "pending"
            else:
                lead.status = "done"

            gestion.closed_lead = (lead.status == "done")
            session.commit()
            log(session, user.id, "LEAD_SAVED", lead.id, detail=chosen.name)
            session.close()
            st.rerun()

    session.close()


# ---------------------------------------------------------------- ADMIN
def admin_view(user):
    session = get_session()
    st.title("🛠️ Panel de Administración")
    tabs = st.tabs(["Dashboard", "Importar Leads", "Asignación", "Tipificaciones", "Usuarios"])

    # ---- Dashboard
    with tabs[0]:
        total = session.query(Lead).count()
        pend = session.query(Lead).filter(Lead.status.in_(["pending", "in_progress"])).count()
        done = session.query(Lead).filter_by(status="done").count()
        ventas = session.query(Gestion).join(Tipificacion).filter(Tipificacion.name == "Venta").count()
        citas = session.query(Gestion).join(Tipificacion).filter(Tipificacion.name == "Cita agendada").count()
        c1, c2, c3, c4, c5 = st.columns(5)
        for c, label, val in zip([c1, c2, c3, c4, c5],
                                  ["Cargados", "Pendientes", "Gestionados", "Ventas", "Citas agendadas"],
                                  [total, pend, done, ventas, citas]):
            c.markdown(f'<div class="metric-card"><h4>{label}</h4><h2>{val}</h2></div>', unsafe_allow_html=True)

        st.divider()
        rows = session.query(Gestion).all()
        if rows:
            df = pd.DataFrame([{
                "Asesor": g.user.name, "Tipificación": g.tipificacion.name,
                "Fecha": g.created_at,
            } for g in rows])
            st.markdown("**Productividad por asesor**")
            st.dataframe(df.groupby("Asesor").size().reset_index(name="Gestiones"), use_container_width=True)
            st.markdown("**Citas agendadas por asesor**")
            citas_df = df[df["Tipificación"] == "Cita agendada"]
            if not citas_df.empty:
                st.dataframe(citas_df.groupby("Asesor").size().reset_index(name="Citas"), use_container_width=True)
            else:
                st.caption("Aún no hay citas agendadas.")
            st.markdown("**Leads por campaña**")
            camp_rows = [{"Campaña": l.campaign.name if l.campaign else "—", "Estado": l.status}
                         for l in session.query(Lead).all()]
            if camp_rows:
                cdf = pd.DataFrame(camp_rows)
                st.dataframe(cdf.groupby(["Campaña", "Estado"]).size().reset_index(name="Leads"),
                             use_container_width=True)
        else:
            st.caption("Aún no hay gestiones registradas.")

    # ---- Importar
    with tabs[1]:
        st.subheader("Importar leads (CSV o Excel)")
        camp_name = st.text_input("Nombre de campaña", value=f"Campaña {dt.date.today()}")
        file = st.file_uploader("Archivo", type=["csv", "xlsx", "xls"])
        if file:
            df = pd.read_csv(file) if file.name.endswith("csv") else pd.read_excel(file)
            st.dataframe(df.head(20), use_container_width=True)
            cols = list(df.columns)
            colA, colB = st.columns(2)
            f_name = colA.selectbox("Columna Nombre", cols)
            f_phone = colB.selectbox("Columna Teléfono", cols)
            colC, colD, colE, colF = st.columns(4)
            f_carrera = colC.selectbox("Columna Carrera", cols)
            f_anos = colD.selectbox("Columna Años de experiencia", cols)
            f_dolor = colE.selectbox("Columna Dolor", cols)
            f_monto = colF.selectbox("Columna Monto económico", cols)

            if st.button("Importar y crear leads"):
                campaign = Campaign(name=camp_name)
                session.add(campaign)
                session.commit()
                mapped = {f_name, f_phone, f_carrera, f_anos, f_dolor, f_monto}
                base_ts = dt.datetime.utcnow().timestamp()
                for i, (_, row) in enumerate(df.iterrows()):
                    extra = {c: str(row[c]) for c in cols if c not in mapped}
                    session.add(Lead(
                        campaign_id=campaign.id,
                        name=str(row.get(f_name, "")),
                        phone=str(row.get(f_phone, "")),
                        carrera=str(row.get(f_carrera, "")),
                        anos_experiencia=str(row.get(f_anos, "")),
                        dolor=str(row.get(f_dolor, "")),
                        monto_economico=str(row.get(f_monto, "")),
                        extra_info=json.dumps(extra),
                        sort_key=base_ts + i * 0.01,
                    ))
                session.commit()
                log(session, user.id, "IMPORT", detail=f"{len(df)} leads -> {camp_name}")
                st.success(f"Se crearon {len(df)} leads en la campaña '{camp_name}'.")

    # ---- Asignación
    with tabs[2]:
        st.subheader("Asignar leads sin asignar")
        unassigned = session.query(Lead).filter_by(assigned_to=None).count()
        st.caption(f"Leads sin asignar: {unassigned}")
        advisors = session.query(User).filter_by(role="asesor", active=True).all()
        if advisors and unassigned:
            mode = st.radio("Modo de asignación", ["Automática (round robin)", "Manual a un asesor"])
            if mode.startswith("Automática"):
                if st.button("Asignar automáticamente"):
                    leads = session.query(Lead).filter_by(assigned_to=None).all()
                    for i, l in enumerate(leads):
                        l.assigned_to = advisors[i % len(advisors)].id
                    session.commit()
                    log(session, user.id, "ASSIGN", detail=f"{len(leads)} leads round robin")
                    st.success(f"{len(leads)} leads asignados.")
            else:
                target = st.selectbox("Asesor destino", [a.name for a in advisors])
                if st.button("Asignar todos los pendientes a este asesor"):
                    leads = session.query(Lead).filter_by(assigned_to=None).all()
                    adv = next(a for a in advisors if a.name == target)
                    for l in leads:
                        l.assigned_to = adv.id
                    session.commit()
                    log(session, user.id, "ASSIGN", detail=f"{len(leads)} leads -> {target}")
                    st.success(f"{len(leads)} leads asignados a {target}.")
        else:
            st.caption("No hay asesores activos o no hay leads pendientes por asignar.")

    # ---- Tipificaciones
    with tabs[3]:
        st.subheader("Tipificaciones existentes")
        tips = session.query(Tipificacion).all()
        st.dataframe(pd.DataFrame([{
            "Nombre": t.name, "Campos requeridos": ", ".join(t.fields()) or "—",
            "Cierra el lead": t.is_final,
        } for t in tips]), use_container_width=True)

        with st.form("new_tip"):
            st.caption("Nueva tipificación")
            name = st.text_input("Nombre")
            fields_raw = st.text_input("Campos adicionales requeridos (separados por coma)", value="")
            is_final = st.checkbox("¿Cierra el lead (no vuelve a la cola)?", value=True)
            if st.form_submit_button("Crear"):
                fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
                session.add(Tipificacion(name=name, required_fields=json.dumps(fields), is_final=is_final))
                session.commit()
                st.success("Tipificación creada.")
                st.rerun()

    # ---- Usuarios
    with tabs[4]:
        st.subheader("Usuarios")
        users = session.query(User).all()
        st.dataframe(pd.DataFrame([{
            "Usuario": u.username, "Nombre": u.name, "Rol": u.role,
            "Correo": u.email or "—", "Activo": u.active,
        } for u in users]), use_container_width=True)
        with st.form("new_user"):
            st.caption("Nuevo usuario")
            uname = st.text_input("Usuario")
            name = st.text_input("Nombre completo")
            pw = st.text_input("Contraseña", type="password")
            role = st.selectbox("Rol", ["asesor", "supervisor", "admin"])
            if st.form_submit_button("Crear usuario"):
                session.add(User(username=uname, name=name, password_hash=hash_pw(pw), role=role))
                session.commit()
                st.success("Usuario creado.")
                st.rerun()

    session.close()


# ---------------------------------------------------------------- SUPERVISOR
def supervisor_view(user):
    session = get_session()
    st.title("📊 Panel de Supervisión")

    advisors = session.query(User).filter_by(role="asesor").all()
    rows = []
    for a in advisors:
        pend = session.query(Lead).filter(
            Lead.assigned_to == a.id, Lead.status.in_(["pending", "in_progress"])).count()
        done = session.query(Gestion).filter_by(user_id=a.id).count()
        ventas = session.query(Gestion).join(Tipificacion).filter(
            Gestion.user_id == a.id, Tipificacion.name == "Venta").count()
        citas = session.query(Gestion).join(Tipificacion).filter(
            Gestion.user_id == a.id, Tipificacion.name == "Cita agendada").count()
        rows.append({"Asesor": a.name, "Pendientes": pend, "Gestionados": done,
                     "Ventas": ventas, "Citas": citas})
    st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.divider()
    st.subheader("Historial de gestiones recientes")
    recent = session.query(Gestion).order_by(Gestion.created_at.desc()).limit(50).all()
    st.dataframe(pd.DataFrame([{
        "Fecha": g.created_at, "Asesor": g.user.name, "Lead": g.lead.name,
        "Tipificación": g.tipificacion.name, "Notas": g.notes,
    } for g in recent]), use_container_width=True)

    st.divider()
    st.subheader("Reasignar leads pendientes")
    lead_options = session.query(Lead).filter(Lead.status != "done").all()
    if lead_options:
        lead_sel = st.selectbox("Lead", [f"#{l.id} - {l.name}" for l in lead_options])
        new_adv = st.selectbox("Nuevo asesor", [a.name for a in advisors])
        if st.button("Reasignar"):
            lid = int(lead_sel.split(" ")[0][1:])
            lead = session.query(Lead).get(lid)
            adv = next(a for a in advisors if a.name == new_adv)
            lead.assigned_to = adv.id
            session.commit()
            log(session, user.id, "REASSIGN", lead.id, detail=f"-> {new_adv}")
            st.success("Lead reasignado.")

    session.close()


# ---------------------------------------------------------------- ROUTER
def main():
    if "user_id" not in st.session_state:
        login_view()
        return

    session = get_session()
    user = session.query(User).get(st.session_state["user_id"])
    session.close()

    with st.sidebar:
        st.markdown(f"**{user.name}**  \n`{user.role}`")
        if st.button("Cerrar sesión"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

    if user.role == "asesor":
        asesor_view(user)
    elif user.role == "admin":
        admin_view(user)
    elif user.role == "supervisor":
        supervisor_view(user)


if __name__ == "__main__":
    main()
