from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, Integer, Boolean, DateTime, String
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, timezone
import os
import io
import pyotp
import qrcode
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, messaging

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ruleta.db")
REGALO_SECRET = os.getenv("REGALO_SECRET", "JBSWY3DPEHPK3PXP")

_sa_path = os.path.join(os.path.dirname(__file__), "firebase-service-account.json")
if os.path.exists(_sa_path) and not firebase_admin._apps:
    firebase_admin.initialize_app(credentials.Certificate(_sa_path))

SCREEN_TAGS = {
    0: "Home",
    1: "Bob Esponja",
    2: "Lotso",
    3: "Angélica",
    4: "Escandalosos",
    5: "Tú y yo",
    6: "Fecha entrega",
    7: "Interacción",
}

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class RuletaEstado(Base):
    __tablename__ = "ruleta_estado"
    id = Column(Integer, primary_key=True, default=1)
    girada = Column(Boolean, default=False, nullable=False)
    girada_en = Column(DateTime(timezone=True), nullable=True)


class RegaloVirtual(Base):
    __tablename__ = "regalo_virtual"
    id = Column(Integer, primary_key=True, default=1)
    desbloqueado = Column(Boolean, default=False, nullable=False)
    desbloqueado_en = Column(DateTime(timezone=True), nullable=True)


class Movimiento(Base):
    __tablename__ = "movimientos"
    id = Column(Integer, primary_key=True, autoincrement=True)
    pantalla = Column(Integer, nullable=False)
    tag = Column(String, nullable=False)
    registrado_en = Column(DateTime(timezone=True), nullable=False)


class FechaEntrega(Base):
    __tablename__ = "fecha_entrega"
    id = Column(Integer, primary_key=True, default=1)
    fecha = Column(String, nullable=True)   # "YYYY-MM-DD"
    hora  = Column(String, nullable=True)   # "HH:MM"
    registrado_en = Column(DateTime(timezone=True), nullable=True)


class FcmToken(Base):
    __tablename__ = "fcm_tokens"
    id = Column(Integer, primary_key=True, default=1)
    token = Column(String, nullable=False)


Base.metadata.create_all(bind=engine)

with SessionLocal() as db:
    if not db.get(RuletaEstado, 1):
        db.add(RuletaEstado(id=1, girada=False, girada_en=None))
        db.commit()
    if not db.get(RegaloVirtual, 1):
        db.add(RegaloVirtual(id=1, desbloqueado=False, desbloqueado_en=None))
        db.commit()
    if not db.get(FechaEntrega, 1):
        db.add(FechaEntrega(id=1, fecha=None, hora=None, registrado_en=None))
        db.commit()


app = FastAPI(
    title="Amor y Amistad — API",
    description="API para la ruleta del amor y el regalo virtual 💘",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Ruleta ────────────────────────────────────────────────────────────────────

@app.get("/ruleta", summary="Estado de la ruleta", tags=["Ruleta"])
def get_estado():
    with SessionLocal() as db:
        estado = db.get(RuletaEstado, 1)
        return {"girada": estado.girada, "girada_en": estado.girada_en}


@app.post("/ruleta/girar", summary="Girar la ruleta", tags=["Ruleta"])
def girar_ruleta():
    with SessionLocal() as db:
        estado = db.get(RuletaEstado, 1)
        if estado.girada:
            return {"ok": False, "detail": "La ruleta ya fue girada.", "girada_en": estado.girada_en}
        estado.girada = True
        estado.girada_en = datetime.now(timezone.utc)
        db.commit()
        db.refresh(estado)
        return {"ok": True, "girada": estado.girada, "girada_en": estado.girada_en}


@app.delete("/ruleta/clear", summary="Resetear la ruleta", tags=["Admin"])
def clear_ruleta():
    with SessionLocal() as db:
        estado = db.get(RuletaEstado, 1)
        estado.girada = False
        estado.girada_en = None
        db.commit()
        return {"ok": True, "detail": "Ruleta reseteada."}


# ── Regalo virtual ────────────────────────────────────────────────────────────

@app.get("/regalo-virtual/qr", summary="QR de desbloqueo", tags=["Regalo virtual"])
def get_qr():
    """Genera un QR con el token TOTP actual. Válido 30s (±30s de margen)."""
    totp = pyotp.TOTP(REGALO_SECRET, interval=30)
    token = totp.now()
    img = qrcode.make(token)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")


@app.post("/regalo-virtual/desbloquear", summary="Desbloquear regalo", tags=["Regalo virtual"])
def desbloquear(token: str):
    """Recibe el token escaneado desde Flutter y lo valida contra TOTP."""
    totp = pyotp.TOTP(REGALO_SECRET, interval=30)
    if not totp.verify(token, valid_window=1):
        return {"ok": False, "detail": "Token inválido o expirado."}
    with SessionLocal() as db:
        estado = db.get(RegaloVirtual, 1)
        if estado.desbloqueado:
            return {"ok": True, "detail": "Ya estaba desbloqueado."}
        estado.desbloqueado = True
        estado.desbloqueado_en = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True, "detail": "Regalo desbloqueado."}


@app.get("/regalo-virtual/estado", summary="Estado del regalo", tags=["Regalo virtual"])
def get_estado_regalo():
    """Polling del frontend para saber si ya fue desbloqueado."""
    with SessionLocal() as db:
        estado = db.get(RegaloVirtual, 1)
        return {"desbloqueado": estado.desbloqueado, "desbloqueado_en": estado.desbloqueado_en}


@app.delete("/regalo-virtual/clear", summary="Resetear regalo", tags=["Admin"])
def clear_regalo():
    with SessionLocal() as db:
        estado = db.get(RegaloVirtual, 1)
        estado.desbloqueado = False
        estado.desbloqueado_en = None
        db.commit()
        return {"ok": True, "detail": "Regalo reseteado."}


# ── Movimientos ───────────────────────────────────────────────────────────────

@app.post("/movimiento", summary="Registrar cambio de pantalla", tags=["Movimientos"])
def registrar_movimiento(pantalla: int):
    tag = SCREEN_TAGS.get(pantalla, f"Pantalla {pantalla}")
    with SessionLocal() as db:
        mov = Movimiento(pantalla=pantalla, tag=tag, registrado_en=datetime.now(timezone.utc))
        db.add(mov)
        db.commit()
        db.refresh(mov)
        return {"ok": True, "id": mov.id, "pantalla": mov.pantalla, "tag": mov.tag, "registrado_en": mov.registrado_en}


@app.get("/movimiento/ultimo", summary="Último movimiento", tags=["Movimientos"])
def get_ultimo_movimiento():
    with SessionLocal() as db:
        mov = db.query(Movimiento).order_by(Movimiento.id.desc()).first()
        if not mov:
            return {"pantalla": None, "tag": None, "registrado_en": None}
        return {"pantalla": mov.pantalla, "tag": mov.tag, "registrado_en": mov.registrado_en}


@app.get("/movimientos", summary="Lista de movimientos", tags=["Movimientos"])
def get_movimientos(limit: int = 30):
    with SessionLocal() as db:
        movs = db.query(Movimiento).order_by(Movimiento.id.desc()).limit(limit).all()
        return [{"id": m.id, "pantalla": m.pantalla, "tag": m.tag, "registrado_en": m.registrado_en} for m in movs]


# ── FCM ───────────────────────────────────────────────────────────────────────

def _send_fcm(title: str, body: str):
    if not firebase_admin._apps:
        return
    with SessionLocal() as db:
        row = db.get(FcmToken, 1)
        if not row:
            return
        token = row.token
    try:
        messaging.send(messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token,
        ))
    except Exception:
        pass


from pydantic import BaseModel

class FcmTokenBody(BaseModel):
    token: str

@app.post("/fcm-token", summary="Registrar token FCM del dispositivo", tags=["FCM"])
def set_fcm_token(body: FcmTokenBody):
    with SessionLocal() as db:
        row = db.get(FcmToken, 1)
        if row:
            row.token = body.token
        else:
            db.add(FcmToken(id=1, token=body.token))
        db.commit()
    return {"ok": True}


# ── Fecha de entrega ──────────────────────────────────────────────────────────

@app.post("/fecha-entrega", summary="Guardar fecha y hora de entrega", tags=["Fecha"])
def set_fecha_entrega(fecha: str, hora: str):
    with SessionLocal() as db:
        row = db.get(FechaEntrega, 1)
        row.fecha = fecha
        row.hora = hora
        row.registrado_en = datetime.now(timezone.utc)
        db.commit()
    _send_fcm("📦 Fecha de entrega", f"El regalo llega el {fecha} a las {hora}")
    return {"ok": True, "fecha": fecha, "hora": hora}


@app.get("/fecha-entrega", summary="Obtener fecha y hora de entrega", tags=["Fecha"])
def get_fecha_entrega():
    with SessionLocal() as db:
        row = db.get(FechaEntrega, 1)
        return {"fecha": row.fecha, "hora": row.hora, "registrado_en": row.registrado_en}


@app.delete("/fecha-entrega", summary="Limpiar fecha de entrega", tags=["Fecha"])
def delete_fecha_entrega():
    with SessionLocal() as db:
        row = db.get(FechaEntrega, 1)
        row.fecha = None
        row.hora = None
        row.registrado_en = None
        db.commit()
    return {"ok": True}


# ── WebSocket — Location tracking ─────────────────────────────────────────────

import json

class ConnectionManager:
    def __init__(self):
        self.subscribers: list[WebSocket] = []

    async def connect_subscriber(self, ws: WebSocket):
        await ws.accept()
        self.subscribers.append(ws)

    def disconnect(self, ws: WebSocket):
        self.subscribers.discard(ws) if hasattr(self.subscribers, 'discard') else None
        if ws in self.subscribers:
            self.subscribers.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.subscribers:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws/location")
async def ws_location(websocket: WebSocket, role: str = "subscriber"):
    if role == "publisher":
        await websocket.accept()
        try:
            while True:
                raw = await websocket.receive_text()
                if raw == "ping":
                    continue
                data = json.loads(raw)
                if "lat" in data and "lng" in data:
                    await manager.broadcast({"lat": data["lat"], "lng": data["lng"]})
        except (WebSocketDisconnect, Exception):
            await manager.broadcast({"active": False})
    else:
        await manager.connect_subscriber(websocket)
        try:
            while True:
                await websocket.receive_text()  # keep-alive, ignore content
        except (WebSocketDisconnect, Exception):
            manager.disconnect(websocket)
