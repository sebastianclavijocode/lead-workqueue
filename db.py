"""
Capa de datos para el Work Queue de leads fríos.
Usa SQLite via SQLAlchemy (fácilmente migrable a PostgreSQL cambiando la URL de conexión).
"""
import datetime as dt
import json
import hashlib
import os

from sqlalchemy import (
    create_engine, Column, Integer, Float, String, DateTime, ForeignKey, Text, Boolean
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "workqueue.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

ROLES = ("admin", "supervisor", "asesor")
NO_CONTESTA_LIMIT = 3  # después de este número de intentos, el lead se cierra como gestionado


def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)  # admin | supervisor | asesor
    email = Column(String, nullable=True)  # correo para recordatorios de "Llamar después"
    active = Column(Boolean, default=True)


class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    leads = relationship("Lead", back_populates="campaign")


class Tipificacion(Base):
    __tablename__ = "tipificaciones"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    # Lista de campos adicionales requeridos, guardada como JSON. Ej: ["fecha","hora","observaciones"]
    required_fields = Column(Text, default="[]")
    is_final = Column(Boolean, default=True)  # si False, el lead vuelve a la cola (ej. "Llamar después")

    def fields(self):
        return json.loads(self.required_fields or "[]")


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"))
    name = Column(String)
    phone = Column(String)
    carrera = Column(String)
    anos_experiencia = Column(String)
    dolor = Column(String)
    monto_economico = Column(String)
    extra_info = Column(Text)  # JSON libre con columnas extra del CSV
    status = Column(String, default="pending")  # pending | in_progress | done
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    sort_key = Column(Float, nullable=True)  # controla el orden dentro de la cola (permite reinserción aleatoria)
    no_contesta_count = Column(Integer, default=0)
    next_follow_up = Column(DateTime, nullable=True)  # para "Llamar después"
    reminder_sent = Column(Boolean, default=False)

    campaign = relationship("Campaign", back_populates="leads")
    gestiones = relationship("Gestion", back_populates="lead", order_by="Gestion.created_at")


class Gestion(Base):
    __tablename__ = "gestiones"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    tipificacion_id = Column(Integer, ForeignKey("tipificaciones.id"))
    notes = Column(Text)
    extra_data = Column(Text)  # JSON: fecha/hora/observaciones u otros campos según tipificación
    closed_lead = Column(Boolean, default=False)  # True solo si esta gestión dejó el lead en 'done'
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    lead = relationship("Lead", back_populates="gestiones")
    tipificacion = relationship("Tipificacion")
    user = relationship("User")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String)  # LEAD_OPENED | LEAD_SAVED | LOGIN | EXPORT | ASSIGN | etc.
    lead_id = Column(Integer, nullable=True)
    detail = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=dt.datetime.utcnow)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        if not session.query(User).first():
            session.add_all([
                User(username="admin", password_hash=hash_pw("admin123"),
                     name="Administrador", role="admin"),
                User(username="supervisor", password_hash=hash_pw("super123"),
                     name="Supervisor Demo", role="supervisor"),
                User(username="asesor1", password_hash=hash_pw("asesor123"),
                     name="Asesor Uno", role="asesor"),
                User(username="asesor2", password_hash=hash_pw("asesor123"),
                     name="Asesor Dos", role="asesor"),
            ])
        if not session.query(Tipificacion).first():
            session.add_all([
                Tipificacion(name="No contesta", required_fields="[]", is_final=False),
                Tipificacion(name="Número incorrecto", required_fields="[]", is_final=True),
                Tipificacion(name="No interesado", required_fields="[]", is_final=True),
                Tipificacion(name="Llamar después",
                              required_fields=json.dumps(["fecha", "hora", "observaciones"]),
                              is_final=False),
                Tipificacion(name="Cita agendada",
                              required_fields=json.dumps(["fecha", "hora"]), is_final=True),
                Tipificacion(name="Venta", required_fields="[]", is_final=True),
                Tipificacion(name="Cliente existente", required_fields="[]", is_final=True),
            ])
        session.commit()
    finally:
        session.close()


def get_session():
    return SessionLocal()
