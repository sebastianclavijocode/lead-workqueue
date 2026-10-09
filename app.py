import datetime as dt
import json
import math
import random
import smtplib
import ssl
from email.mime.text import MIMEText

import pandas as pd
import streamlit as st
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from db import (
    init_db, get_session, hash_pw, NO_CONTESTA_LIMIT, NO_CONTESTA_GAP,
    now_local, to_local, local_day_start_utc,
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
def smtp_status() -> dict:
    """Indica qué parte de la configuración SMTP está presente en los secrets (sin exponer la contraseña)."""
    try:
        cfg = st.secrets.get("smtp") or {}
    except Exception:
        cfg = {}
    return {k: bool(cfg.get(k)) for k in ("host", "user", "password")}


def get_smtp_config():
    """Lee la configuración SMTP desde los secrets ([smtp]). Devuelve None si está incompleta."""
    try:
        cfg = st.secrets.get("smtp")
        if cfg and cfg.get("host") and cfg.get("user") and cfg.get("password"):
            return cfg
    except Exception:
        return None
    return None


def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Envía un correo por SMTP. Devuelve (ok, mensaje); el mensaje trae el error real si falla."""
    cfg = get_smtp_config()
    if not cfg:
        faltan = [k for k, ok in smtp_status().items() if not ok]
        return False, ("SMTP no configurado: faltan en Secrets los campos "
                       f"{', '.join(faltan) or 'host/user/password'} dentro de [smtp].")
    try:
        sender = cfg.get("from") or cfg["user"]
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to_email
        port = int(cfg.get("port", 587))
        password = str(cfg["password"]).replace(" ", "")  # las claves de app de Gmail vienen con espacios
        if port == 465:
            with smtplib.SMTP_SSL(cfg["host"], port, timeout=20,
                                  context=ssl.create_default_context()) as server:
                server.login(cfg["user"], password)
                server.sendmail(sender, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(cfg["host"], port, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(cfg["user"], password)
                server.sendmail(sender, [to_email], msg.as_string())
        return True, f"Enviado a {to_email}."
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_due_reminders(session, user):
    """Envía los recordatorios de 'Llamar después' cuya fecha/hora (hora local) ya se cumplió.
    Se ejecuta cada vez que el asesor abre/recarga la app (no es un scheduler en segundo plano).
    Devuelve (enviados, errores) para poder mostrarlo en pruebas manuales."""
    if not user.email:
        return 0, ["El asesor no tiene correo guardado."]
    due = (session.query(Lead)
           .filter(Lead.assigned_to == user.id, Lead.reminder_sent == False,  # noqa: E712
                   Lead.next_follow_up.isnot(None),
                   Lead.next_follow_up <= now_local())
           .all())
    sent, errors = 0, []
    for lead in due:
        ok, msg = send_email(
            user.email,
            f"Recordatorio de seguimiento — {lead.name or 'Lead #' + str(lead.id)}",
            f"Es hora de contactar de nuevo a {lead.name} ({lead.phone}).\n"
            f"Programado para: {lead.next_follow_up:%Y-%m-%d %H:%M}\n"
            f"Carrera: {lead.carrera or '-'} | Dolor: {lead.dolor or '-'}",
        )
        lead.reminder_sent = ok  # si falla el envío, se reintenta en el siguiente check
        if ok:
            sent += 1
        else:
            errors.append(msg)
    if due:
        session.commit()
    return sent, errors


def _eligible_other_keys(session, user_id, exclude_lead_id):
    """sort_key (ordenados) de los demás leads que el asesor podría ver ahora mismo en su cola."""
    rows = (session.query(Lead.sort_key)
            .filter(Lead.assigned_to == user_id, Lead.status == "pending",
                    Lead.id != exclude_lead_id,
                    or_(Lead.next_follow_up.is_(None), Lead.next_follow_up <= now_local()))
            .order_by(Lead.sort_key.asc()).all())
    return [r[0] if r[0] is not None else 0.0 for r in rows]


def position_after_n(session, user_id, exclude_lead_id, n):
    """sort_key para que el lead reaparezca después de n leads más de la cola del asesor
    (usado para 'No contesta': vuelve cada NO_CONTESTA_GAP leads). Si la cola tiene menos de
    n leads, queda al final."""
    keys = _eligible_other_keys(session, user_id, exclude_lead_id)
    if not keys:
        return dt.datetime.utcnow().timestamp()
    if len(keys) <= n:
        return keys[-1] + 1.0
    return (keys[n - 1] + keys[n]) / 2


def front_sort_key(session, user_id, exclude_lead_id):
    """sort_key que pone el lead de primero en la cola (para seguimientos 'Llamar después' ya vencidos)."""
    rows = (session.query(Lead.sort_key)
            .filter(Lead.assigned_to == user_id, Lead.id != exclude_lead_id)
            .order_by(Lead.sort_key.asc()).first())
    first = rows[0] if rows and rows[0] is not None else 0.0
    return first - 1.0


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

    flash = st.session_state.pop("flash", None)
    if flash:
        st.warning(flash) if flash.startswith("⚠️") else st.success(flash)

    if user.email:
        _, reminder_errors = send_due_reminders(session, user)
        if reminder_errors:
            st.warning("⚠️ Hay un recordatorio vencido que no se pudo enviar por correo: "
                       f"{reminder_errors[0]}")

    with st.expander("✉️ Mi correo para recordatorios de seguimiento"):
        email_input = st.text_input("Correo", value=user.email or "", key="email_input")
        col_save, col_test = st.columns(2)
        if col_save.button("Guardar correo"):
            user.email = email_input.strip()
            session.commit()
            st.success("Correo actualizado.")
        if col_test.button("📨 Enviar correo de prueba"):
            target = (email_input or user.email or "").strip()
            if not target:
                st.error("Escribe y guarda tu correo primero.")
            else:
                ok, msg = send_email(target, "Prueba de recordatorios — Work Queue",
                                     "Si recibes este correo, los recordatorios de seguimiento funcionan.")
                st.success(f"✅ {msg}") if ok else st.error(f"❌ No se pudo enviar: {msg}")

    pending_count = session.query(Lead).filter_by(assigned_to=user.id, status="pending").count()
    in_progress_count = session.query(Lead).filter_by(assigned_to=user.id, status="in_progress").count()
    done_today = session.query(Gestion).filter(
        Gestion.user_id == user.id,
        Gestion.closed_lead == True,  # noqa: E712 — solo cierres reales, no intentos como "No contesta"
        Gestion.created_at >= local_day_start_utc(now_local().date()),
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
                .filter(Lead.assigned_to == user.id, Lead.status == "pending",
                        or_(Lead.next_follow_up.is_(None),
                            Lead.next_follow_up <= now_local()))
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
                    lead.next_follow_up = None
                    lead.sort_key = position_after_n(session, user.id, lead.id, NO_CONTESTA_GAP)

            elif chosen.name == "Llamar después" and extra_values.get("fecha"):
                lead.status = "pending"
                fecha = extra_values["fecha"]
                hora = extra_values.get("hora") or "00:00:00"
                try:
                    lead.next_follow_up = dt.datetime.fromisoformat(f"{fecha}T{hora}")
                except ValueError:
                    lead.next_follow_up = dt.datetime.fromisoformat(fecha)
                lead.reminder_sent = False
                # Al vencer la fecha, el lead debe ser el siguiente en aparecer (no perderse en la cola)
                lead.sort_key = front_sort_key(session, user.id, lead.id)
                if user.email:
                    ok_mail, msg_mail = send_email(
                        user.email,
                        f"Seguimiento programado — {lead.name or 'Lead #' + str(lead.id)}",
                        f"Se programó un recordatorio para contactar a {lead.name} ({lead.phone}) "
                        f"el {lead.next_follow_up:%Y-%m-%d %H:%M}.",
                    )
                    if not ok_mail:
                        st.session_state["flash"] = f"⚠️ Seguimiento guardado, pero el correo no se envió: {msg_mail}"
                else:
                    st.session_state["flash"] = ("⚠️ Seguimiento guardado, pero no tienes correo registrado "
                                                 "para recibir el recordatorio.")

            elif not chosen.is_final:
                lead.status = "pending"
                lead.next_follow_up = None
                lead.reminder_sent = False
            else:
                lead.status = "done"
                lead.next_follow_up = None

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
    tabs = st.tabs(["Dashboard", "Importar Leads", "Asignación", "Tipificaciones", "Usuarios", "🧹 Mantenimiento"])

    # ---- Dashboard
    with tabs[0]:
        today = now_local().date()
        period = st.radio("Período", ["Hoy", "Ayer", "Últimos 7 días", "Últimos 30 días", "Personalizado", "Todo"],
                          horizontal=True, key="dash_period")
        if period == "Hoy":
            d_from = d_to = today
        elif period == "Ayer":
            d_from = d_to = today - dt.timedelta(days=1)
        elif period == "Últimos 7 días":
            d_from, d_to = today - dt.timedelta(days=6), today
        elif period == "Últimos 30 días":
            d_from, d_to = today - dt.timedelta(days=29), today
        elif period == "Personalizado":
            rng = st.date_input("Rango de fechas", value=(today - dt.timedelta(days=6), today),
                                key="dash_range")
            if isinstance(rng, (tuple, list)):
                d_from = rng[0] if rng else today
                d_to = rng[1] if len(rng) > 1 else d_from
            else:
                d_from = d_to = rng
        else:
            d_from = d_to = None

        gq = session.query(Gestion).options(
            joinedload(Gestion.lead), joinedload(Gestion.user), joinedload(Gestion.tipificacion))
        if d_from:
            gq = gq.filter(Gestion.created_at >= local_day_start_utc(d_from),
                           Gestion.created_at < local_day_start_utc(d_to + dt.timedelta(days=1)))
            st.caption(f"Mostrando gestiones del {d_from:%d/%m/%Y} al {d_to:%d/%m/%Y} (hora Colombia).")
        gestiones = gq.all()

        total = session.query(Lead).count()
        pend = session.query(Lead).filter(Lead.status.in_(["pending", "in_progress"])).count()
        cerrados = sum(1 for g in gestiones if g.closed_lead)
        ventas = sum(1 for g in gestiones if g.tipificacion.name == "Venta")
        citas = sum(1 for g in gestiones if g.tipificacion.name == "Cita agendada")
        c1, c2, c3, c4, c5 = st.columns(5)
        for c, label, val in zip([c1, c2, c3, c4, c5],
                                  ["Cargados (total)", "Pendientes (ahora)", "Gestionados", "Ventas", "Citas agendadas"],
                                  [total, pend, cerrados, ventas, citas]):
            c.markdown(f'<div class="metric-card"><h4>{label}</h4><h2>{val}</h2></div>', unsafe_allow_html=True)
        st.caption("Cargados y Pendientes son el estado actual; Gestionados, Ventas y Citas corresponden al período elegido.")

        st.divider()
        if gestiones:
            df = pd.DataFrame([{
                "Asesor": g.user.name, "Tipificación": g.tipificacion.name, "Cerró lead": bool(g.closed_lead),
            } for g in gestiones])
            st.markdown("**Productividad por asesor**")
            prod = df.groupby("Asesor").agg(
                Gestiones=("Tipificación", "size"),
                Gestionados=("Cerró lead", "sum"),
                Ventas=("Tipificación", lambda x: int((x == "Venta").sum())),
                Citas=("Tipificación", lambda x: int((x == "Cita agendada").sum())),
            ).reset_index()
            st.dataframe(prod, use_container_width=True)

            st.markdown("**Resultados por tipificación**")
            st.dataframe(df["Tipificación"].value_counts().rename_axis("Tipificación")
                         .reset_index(name="Cantidad"), use_container_width=True)

            st.markdown("**Citas agendadas — detalle**")
            detail = []
            for g in gestiones:
                if g.tipificacion.name != "Cita agendada":
                    continue
                ed = json.loads(g.extra_data or "{}")
                detail.append({
                    "Lead": g.lead.name, "Teléfono": g.lead.phone, "Asesor": g.user.name,
                    "Fecha cita": ed.get("fecha", "—"), "Hora cita": ed.get("hora", "—"),
                    "Registrado": to_local(g.created_at).strftime("%Y-%m-%d %H:%M"),
                })
            if detail:
                st.dataframe(pd.DataFrame(detail).sort_values("Fecha cita"), use_container_width=True)
            else:
                st.caption("No hay citas agendadas en este período.")

            st.markdown("**Detalle de gestiones del período**")
            only_closed = st.checkbox("Solo los que cerraron el lead (gestionados)", value=True, key="dash_only_closed")
            rows = [{
                "Fecha": to_local(g.created_at).strftime("%Y-%m-%d %H:%M"),
                "Lead": g.lead.name, "Teléfono": g.lead.phone, "Asesor": g.user.name,
                "Tipificación": g.tipificacion.name, "Cerró lead": "Sí" if g.closed_lead else "No",
                "Notas": g.notes or "",
            } for g in sorted(gestiones, key=lambda x: x.created_at, reverse=True)
                if g.closed_lead or not only_closed]
            if rows:
                ddf = pd.DataFrame(rows)
                st.dataframe(ddf, use_container_width=True)
                st.download_button("⬇️ Descargar detalle (CSV)", ddf.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"gestiones_{period.lower().replace(' ', '_')}.csv", mime="text/csv")
            else:
                st.caption("No hay gestiones que cumplan ese filtro.")
        else:
            st.caption("No hay gestiones registradas en este período.")

        st.markdown("**Leads por campaña**")
        camp_rows = [{"Campaña": l.campaign.name if l.campaign else "—", "Estado": l.status}
                     for l in session.query(Lead).options(joinedload(Lead.campaign)).all()]
        if camp_rows:
            cdf = pd.DataFrame(camp_rows)
            st.dataframe(cdf.groupby(["Campaña", "Estado"]).size().reset_index(name="Leads"),
                         use_container_width=True)

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

        st.divider()
        st.subheader("🔁 Reasignación masiva entre asesores")
        st.caption("Mueve de un asesor a otro aunque los leads ya estén asignados.")
        advisors_all = session.query(User).filter_by(role="asesor", active=True).all()
        if len(advisors_all) >= 2:
            colA, colB = st.columns(2)
            origin_name = colA.selectbox("Desde (asesor origen)", [a.name for a in advisors_all],
                                          key="bulk_origin")
            dest_options = [a.name for a in advisors_all if a.name != origin_name]
            dest_name = colB.selectbox("Hacia (asesor destino)", dest_options, key="bulk_dest")
            include_done = st.checkbox("Incluir también los leads ya gestionados (cerrados)", value=False)

            origin = next(a for a in advisors_all if a.name == origin_name)
            dest = next(a for a in advisors_all if a.name == dest_name)
            query = session.query(Lead).filter(Lead.assigned_to == origin.id)
            if not include_done:
                query = query.filter(Lead.status.in_(["pending", "in_progress"]))
            total_available = query.count()

            pct = st.radio("Porcentaje a mover", [25, 50, 75, 100], horizontal=True,
                           index=3, key="bulk_pct")
            count_to_move = math.ceil(total_available * pct / 100) if total_available else 0

            st.caption(f"De {origin_name} tiene **{total_available}** leads disponibles → "
                       f"se moverían **{count_to_move}** ({pct}%), elegidos al azar.")
            confirm_bulk = st.text_input("Escribe MOVER para confirmar", key="confirm_bulk_reassign")
            if st.button(f"🔁 Mover {count_to_move} leads ({pct}%) de {origin_name} a {dest_name}",
                        type="primary"):
                if confirm_bulk.strip().upper() != "MOVER":
                    st.error("Escribe exactamente MOVER en el campo de confirmación.")
                elif count_to_move == 0:
                    st.warning("No hay leads que mover con esos filtros.")
                else:
                    if pct == 100:
                        target_ids = [l.id for l in query.all()]
                    else:
                        all_ids = [l.id for l in query.all()]
                        target_ids = random.sample(all_ids, count_to_move)
                    n = (session.query(Lead).filter(Lead.id.in_(target_ids))
                         .update({Lead.assigned_to: dest.id}, synchronize_session=False))
                    session.commit()
                    log(session, user.id, "REASSIGN_BULK",
                        detail=f"{n} leads ({pct}%): {origin_name} -> {dest_name} "
                               f"(incluye cerrados: {include_done})")
                    st.success(f"{n} leads movidos de {origin_name} a {dest_name}.")
                    st.rerun()
        else:
            st.caption("Necesitas al menos 2 asesores activos para reasignar en bloque.")

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

    # ---- Mantenimiento
    with tabs[5]:
        st.subheader("✉️ Diagnóstico de correo")
        status = smtp_status()
        st.caption("Configuración SMTP detectada en Secrets: "
                   + " · ".join(f"{'✅' if ok else '❌'} {k}" for k, ok in status.items()))
        if not all(status.values()):
            st.warning("Falta configuración: en Streamlit Cloud → ⋮ → Settings → Secrets agrega el bloque "
                       "[smtp] con host, port, user y password (ver README).")
        test_to = st.text_input("Enviar correo de prueba a", value=user.email or "", key="admin_test_mail")
        if st.button("📨 Enviar correo de prueba", key="admin_send_test"):
            if not test_to.strip():
                st.error("Escribe un correo de destino.")
            else:
                ok, msg = send_email(test_to.strip(), "Prueba de correo — Work Queue",
                                     "Si recibes este mensaje, el envío de correos está bien configurado.")
                st.success(f"✅ {msg}") if ok else st.error(f"❌ No se pudo enviar: {msg}")
        if st.button("🔔 Revisar y enviar recordatorios vencidos ahora", key="admin_send_due"):
            total_sent, all_errors, sin_correo = 0, [], []
            for adv in session.query(User).filter_by(role="asesor", active=True).all():
                if not adv.email:
                    sin_correo.append(adv.name)
                    continue
                n_sent, errs = send_due_reminders(session, adv)
                total_sent += n_sent
                all_errors += [f"{adv.name}: {e}" for e in errs]
            st.success(f"Recordatorios enviados: {total_sent}.")
            if all_errors:
                st.error("Errores: " + " | ".join(all_errors))
            if sin_correo:
                st.info("Asesores sin correo guardado (no se les envía): " + ", ".join(sin_correo))

        st.divider()
        st.subheader("🧹 Eliminar una campaña subida por error")
        st.caption("Borra la campaña, sus leads y las gestiones asociadas. Úsalo cuando una "
                   "importación quedó mal (columnas cruzadas, archivo equivocado, duplicados, etc.)")

        campaigns = session.query(Campaign).all()
        if campaigns:
            camp_labels = {
                f"{c.name} (#{c.id}) — {session.query(Lead).filter_by(campaign_id=c.id).count()} leads": c.id
                for c in campaigns
            }
            sel_label = st.selectbox("Campaña a eliminar", list(camp_labels.keys()), key="del_camp_sel")
            sel_id = camp_labels[sel_label]

            confirm_text = st.text_input(
                "Escribe ELIMINAR para confirmar (esta acción no se puede deshacer)",
                key="confirm_delete_campaign",
            )
            if st.button("🗑️ Eliminar campaña seleccionada", type="primary"):
                if confirm_text.strip().upper() != "ELIMINAR":
                    st.error("Escribe exactamente ELIMINAR en el campo de confirmación.")
                else:
                    lead_ids = [l.id for l in session.query(Lead.id).filter_by(campaign_id=sel_id).all()]
                    n_gestiones = session.query(Gestion).filter(Gestion.lead_id.in_(lead_ids)).delete(
                        synchronize_session=False)
                    n_leads = session.query(Lead).filter_by(campaign_id=sel_id).delete(synchronize_session=False)
                    session.query(Campaign).filter_by(id=sel_id).delete(synchronize_session=False)
                    session.commit()
                    log(session, user.id, "DELETE_CAMPAIGN",
                        detail=f"campaign_id={sel_id}, {n_leads} leads, {n_gestiones} gestiones")
                    st.success(f"Campaña eliminada: {n_leads} leads y {n_gestiones} gestiones borrados.")
                    st.rerun()
        else:
            st.caption("No hay campañas cargadas todavía.")

        st.divider()
        st.subheader("⚠️ Zona de peligro: vaciar toda la base de leads")
        st.caption("Borra TODAS las campañas, leads y gestiones. Los usuarios y tipificaciones "
                   "no se tocan. Úsalo solo si necesitas empezar de cero.")
        confirm_all = st.text_input("Escribe BORRAR TODO para confirmar", key="confirm_wipe_all")
        if st.button("🗑️ Vaciar toda la base de leads"):
            if confirm_all.strip().upper() != "BORRAR TODO":
                st.error("Escribe exactamente BORRAR TODO en el campo de confirmación.")
            else:
                n_g = session.query(Gestion).delete(synchronize_session=False)
                n_l = session.query(Lead).delete(synchronize_session=False)
                n_c = session.query(Campaign).delete(synchronize_session=False)
                session.commit()
                log(session, user.id, "WIPE_ALL_LEADS", detail=f"{n_l} leads, {n_c} campañas, {n_g} gestiones")
                st.success(f"Base vaciada: {n_l} leads, {n_c} campañas, {n_g} gestiones eliminados.")
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
