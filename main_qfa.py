# ============================================================
# QueFiestaApp — Backend FastAPI
# Un producto de GestionaTeIA
# Propietaria: Claudia Alonso
# ============================================================
# REGLAS IMPORTANTES:
# - NUNCA usar db.close() — siempre usar release_db(db) en finally
# - Supabase usa Session Pooler (no conexión directa) por IPv4
# - Todos los endpoints bajo /quefiestaapp/
# ============================================================

import os
import asyncio
import asyncpg
import bcrypt
import jwt
import json
from datetime import datetime, date, timedelta
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import httpx
import uuid

import resend

# ============================================================
# CONFIGURACIÓN
# ============================================================

QFA_DB_HOST     = os.environ.get("QFA_DB_HOST", "aws-1-us-east-1.pooler.supabase.com")
QFA_DB_PORT     = int(os.environ.get("QFA_DB_PORT", "5432"))
QFA_DB_NAME     = os.environ.get("QFA_DB_NAME", "postgres")
QFA_DB_USER     = os.environ.get("QFA_DB_USER", "postgres.yvcrgauscaghtfltvhbc")
QFA_DB_PASSWORD = os.environ.get("QFA_DB_PASSWORD", "")
QFA_JWT_SECRET  = os.environ.get("QFA_JWT_SECRET", "qfa_secret_cambiar_en_produccion")
QFA_SUPERADMIN_PASSWORD = os.environ.get("QFA_SUPERADMIN_PASSWORD", "qfaSuperAdmin2026!")
QFA_RESEND_API_KEY = os.environ.get("QFA_RESEND_API_KEY", "")
QFA_SUPABASE_URL = os.environ.get("QFA_SUPABASE_URL", "https://yvcrgauscaghtfltvhbc.supabase.co")
QFA_SUPABASE_SERVICE_KEY = os.environ.get("QFA_SUPABASE_SERVICE_KEY", "")

# Pool de conexiones
qfa_pool = None

# ============================================================
# POOL DE BASE DE DATOS
# ============================================================

async def init_qfa_pool():
    global qfa_pool
    qfa_pool = await asyncpg.create_pool(
        host=QFA_DB_HOST,
        port=QFA_DB_PORT,
        database=QFA_DB_NAME,
        user=QFA_DB_USER,
        password=QFA_DB_PASSWORD,
        min_size=1,
        max_size=5,
        ssl="require"
    )

async def get_qfa_db():
    return await qfa_pool.acquire()

def release_db(db):
    asyncio.ensure_future(qfa_pool.release(db))

# ============================================================
# EMAILS — via Resend desde hola@gestionateia.com
# ============================================================

def enviar_email_qfa(destinatario: str, asunto: str, cuerpo_html: str):
    """Envía un email desde hola@gestionateia.com via Resend."""
    try:
        resend.api_key = QFA_RESEND_API_KEY
        resend.Emails.send({
            "from": "QueFiestaApp · GestionaTeIA <hola@gestionateia.com>",
            "to": destinatario,
            "subject": asunto,
            "html": cuerpo_html
        })
    except Exception as e:
        print(f"[QFA EMAIL] Error enviando a {destinatario}: {e}")


def email_confirmacion_reserva(
    cliente_nombre: str,
    cliente_email: str,
    salon_nombre: str,
    fecha: str,
    hora_inicio: str,
    hora_fin: str,
    cantidad_ninos: int,
    nombre_festejado: str,
    menu_seleccionado: list,
    juegos_seleccionados: list,
    precio_total: float,
    monto_seña: float,
    modalidad_cobro: str,
    alias_transferencia: str,
    mensaje_pago: str,
    whatsapp_salon: str
):
    """Email de confirmación de reserva al cliente."""

    # Formatear precio
    def fmt(n): return f"${int(n):,}".replace(",", ".")

    # Armar detalle de menú
    menu_html = ""
    if menu_seleccionado:
        items = "".join([f"<li style='margin-bottom:4px;'>✓ {m['nombre']}</li>" for m in menu_seleccionado])
        menu_html = f"<div style='margin:12px 0;'><strong>Menú seleccionado:</strong><ul style='margin:8px 0 0 16px;color:#4A5568;'>{items}</ul></div>"

    # Armar detalle de juegos
    juegos_html = ""
    if juegos_seleccionados:
        items = "".join([f"<li style='margin-bottom:4px;'>✓ {j['nombre']}</li>" for j in juegos_seleccionados])
        juegos_html = f"<div style='margin:12px 0;'><strong>Juegos y adicionales:</strong><ul style='margin:8px 0 0 16px;color:#4A5568;'>{items}</ul></div>"

    # Info de pago
    pago_html = ""
    if modalidad_cobro and modalidad_cobro != "mercadopago":
        seña_info = f"<p style='margin:8px 0;'>💰 <strong>Seña a abonar: {fmt(monto_seña)}</strong></p>" if monto_seña > 0 else ""
        alias_info = f"<p style='margin:8px 0;'>🏦 Alias / CBU: <strong>{alias_transferencia}</strong></p>" if alias_transferencia else ""
        mensaje_info = f"<p style='margin:12px 0;color:#4A5568;'>{mensaje_pago}</p>" if mensaje_pago else ""
        pago_html = f"""
        <div style='background:#FFF9E6;border:1px solid #FFD93D;border-radius:10px;padding:16px;margin:20px 0;'>
            <p style='margin:0 0 8px;font-weight:bold;color:#2C3E50;'>💳 Instrucciones de pago</p>
            {seña_info}{alias_info}{mensaje_info}
        </div>"""

    # WhatsApp
    wa_html = ""
    if whatsapp_salon:
        wa_html = f"<p style='margin:8px 0;'>📱 WhatsApp del salón: <a href='https://wa.me/{whatsapp_salon}' style='color:#71D997;'>{whatsapp_salon}</a></p>"

    festejado_html = f"<p style='margin:8px 0;'>🎂 Festejado/a: <strong>{nombre_festejado}</strong></p>" if nombre_festejado else ""

    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;'>
        <div style='background:linear-gradient(135deg,#ff6b6b,#ffd93d);padding:32px;border-radius:12px 12px 0 0;text-align:center;'>
            <div style='font-family:Georgia,serif;font-size:28px;font-weight:900;color:white;'>🎉 ¡Solicitud recibida!</div>
        </div>
        <div style='background:#F9F9FB;padding:28px;border-radius:0 0 12px 12px;'>
            <p style='font-size:15px;color:#2C3E50;'>Hola <strong>{cliente_nombre}</strong>,</p>
            <p style='color:#4A5568;line-height:1.7;'>
                Tu solicitud de reserva en <strong>{salon_nombre}</strong> fue recibida correctamente.
                A continuación el resumen:
            </p>

            <div style='background:white;border:1px solid #E2E8F0;border-radius:10px;padding:20px;margin:20px 0;'>
                <p style='margin:0 0 12px;font-weight:bold;color:#2C3E50;font-size:15px;'>📋 Detalle de tu reserva</p>
                <p style='margin:8px 0;'>📅 <strong>Fecha:</strong> {fecha}</p>
                <p style='margin:8px 0;'>🕐 <strong>Horario:</strong> {hora_inicio[:5]} - {hora_fin[:5]}</p>
                <p style='margin:8px 0;'>👶 <strong>Cantidad de niños:</strong> {cantidad_ninos}</p>
                {festejado_html}
                {menu_html}
                {juegos_html}
                <div style='border-top:2px solid #E2E8F0;margin-top:16px;padding-top:16px;'>
                    <p style='margin:0;font-size:18px;font-weight:bold;color:#ff6b6b;'>
                        Total estimado: {fmt(precio_total)}
                    </p>
                </div>
            </div>

            {pago_html}
            {wa_html}

            <p style='color:#4A5568;font-size:14px;line-height:1.6;margin-top:20px;'>
                El salón revisará tu solicitud y comprobante de pago a la brevedad.
                Una vez confirmado, recibirás un email de confirmación.
            </p>

            <div style='text-align:center;margin-top:24px;padding-top:20px;border-top:1px solid #E2E8F0;'>
                <p style='font-size:12px;color:#999;margin:0;'>
                    Un producto de
                    <strong style='font-family:Arial;'>
                        <span style='color:#2C3E50;'>Gestiona</span><span style='color:#FF8000;'>Te</span><span style='color:#71D997;'>IA</span>
                    </strong>
                    · <a href='https://gestionateia.com' style='color:#999;'>gestionateia.com</a>
                </p>
            </div>
        </div>
    </div>
    """

    enviar_email_qfa(cliente_email, f"Solicitud de reserva recibida — {salon_nombre}", html)


def email_bienvenida_tenant_qfa(
    nombre_salon: str,
    email_admin: str,
    slug: str,
    password: str,
    trial_hasta: str
):
    """Email de bienvenida al nuevo tenant con sus datos de acceso."""

    url_admin = f"https://quefiestaapp.gestionateia.com/admin/{slug}"
    url_publica = f"https://quefiestaapp.gestionateia.com/{slug}"

    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;'>
        <div style='background:#1a1a2e;padding:32px;border-radius:12px 12px 0 0;text-align:center;'>
            <div style='font-family:Georgia,serif;font-size:26px;font-weight:900;'>
                <span style='color:#ffffff;'>¡Bienvenido a </span><span style='color:#ff6b6b;'>Que</span><span style='color:#ffd93d;'>Fiesta</span><span style='color:#71D997;'>App</span><span style='color:#ffffff;'>!</span>
            </div>
        </div>
        <div style='background:#F9F9FB;padding:28px;border-radius:0 0 12px 12px;'>
            <p style='font-size:15px;color:#2C3E50;'>Hola <strong>{nombre_salon}</strong>,</p>
            <p style='color:#4A5568;line-height:1.7;'>
                Tu cuenta en <strong>QueFiestaApp</strong> fue creada exitosamente.
                Tenés <strong>30 días de prueba gratuita</strong> hasta el <strong>{trial_hasta}</strong>.
            </p>

            <div style='background:white;border:2px solid #ff6b6b;border-radius:10px;padding:20px;margin:24px 0;'>
                <p style='margin:0 0 12px;font-weight:bold;color:#2C3E50;font-size:15px;'>🔑 Tus datos de acceso</p>
                <table style='width:100%;border-collapse:collapse;'>
                    <tr>
                        <td style='padding:8px 0;color:#7f8c8d;font-size:13px;width:140px;'>Panel admin:</td>
                        <td style='padding:8px 0;'>
                            <a href='{url_admin}' style='color:#ff6b6b;font-weight:bold;font-size:14px;'>{url_admin}</a>
                        </td>
                    </tr>
                    <tr style='background:#f9f9fb;'>
                        <td style='padding:8px 0;color:#7f8c8d;font-size:13px;'>Tu ID de salón:</td>
                        <td style='padding:8px 0;color:#2C3E50;font-weight:bold;font-family:monospace;font-size:15px;'>{slug}</td>
                    </tr>
                    <tr>
                        <td style='padding:8px 0;color:#7f8c8d;font-size:13px;'>Contraseña:</td>
                        <td style='padding:8px 0;color:#2C3E50;font-weight:bold;font-family:monospace;font-size:15px;'>{password}</td>
                    </tr>
                    <tr style='background:#f9f9fb;'>
                        <td style='padding:8px 0;color:#7f8c8d;font-size:13px;'>Tu página pública:</td>
                        <td style='padding:8px 0;'>
                            <a href='{url_publica}' style='color:#ff6b6b;font-weight:bold;font-size:14px;'>{url_publica}</a>
                        </td>
                    </tr>
                </table>
            </div>

            <p style='color:#E74C3C;font-size:13px;font-weight:bold;'>
                ⚠️ Por seguridad, te recomendamos cambiar tu contraseña desde el panel admin después del primer ingreso.
            </p>

            <div style='text-align:center;margin-top:24px;'>
                <a href='{url_admin}'
                   style='background:#ff6b6b;color:white;padding:14px 32px;border-radius:50px;
                          text-decoration:none;font-weight:bold;font-size:15px;display:inline-block;'>
                    Ir al panel de administración →
                </a>
            </div>

            <div style='text-align:center;margin-top:24px;padding-top:20px;border-top:1px solid #E2E8F0;'>
                <p style='font-size:12px;color:#999;margin:0;'>
                    Un producto de
                    <strong style='font-family:Arial;'>
                        <span style='color:#2C3E50;'>Gestiona</span><span style='color:#FF8000;'>Te</span><span style='color:#71D997;'>IA</span>
                    </strong>
                    · <a href='https://gestionateia.com/soporte' style='color:#999;'>Soporte</a>
                </p>
            </div>
        </div>
    </div>
    """

    enviar_email_qfa(email_admin, f"¡Bienvenido a QueFiestaApp! — Tus datos de acceso", html)

# ============================================================
# APP FASTAPI
# ============================================================

qfa_app = FastAPI(title="QueFiestaApp API", version="1.0.0")

qfa_app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://quefiestaapp.gestionateia.com",
        "https://www.quefiestaapp.gestionateia.com",
        "https://gestionateia.com",
        "https://www.gestionateia.com",
        "https://reservatuespacio.com",
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token", "X-Superadmin-Token", "Authorization"],
)

@qfa_app.on_event("startup")
async def startup():
    await init_qfa_pool()

# ============================================================
# HELPERS DE AUTENTICACIÓN
# ============================================================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_token(tenant_id: str, slug: str, rol: str = "admin") -> str:
    payload = {
        "tenant_id": tenant_id,
        "slug": slug,
        "rol": rol,
        "exp": datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, QFA_JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, QFA_JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

async def get_admin_token(x_admin_token: str = Header(...)):
    return verify_token(x_admin_token)

async def get_superadmin_token(x_superadmin_token: str = Header(...)):
    if x_superadmin_token != QFA_SUPERADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Token superadmin inválido")
    return True

# ============================================================
# MODELOS PYDANTIC
# ============================================================

class LoginRequest(BaseModel):
    slug: str
    password: str

class MenuItemCreate(BaseModel):
    nombre: str
    descripcion: Optional[str] = None
    precio_base: float
    precio_por_nino_extra: float
    imagen_url: Optional[str] = None
    activo: bool = True
    orden: int = 0

class MenuItemUpdate(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    precio_base: Optional[float] = None
    precio_por_nino_extra: Optional[float] = None
    imagen_url: Optional[str] = None
    activo: Optional[bool] = None
    orden: Optional[int] = None

class JuegoCreate(BaseModel):
    nombre: str
    descripcion: Optional[str] = None
    precio_fijo: float = 0
    precio_por_nino: float = 0
    imagen_url: Optional[str] = None
    activo: bool = True
    orden: int = 0

class JuegoUpdate(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    precio_fijo: Optional[float] = None
    precio_por_nino: Optional[float] = None
    imagen_url: Optional[str] = None
    activo: Optional[bool] = None
    orden: Optional[int] = None

class HorarioCreate(BaseModel):
    dia_semana: int  # 0=domingo, 6=sábado
    hora_inicio: str  # "15:00"
    hora_fin: str     # "20:00"
    activo: bool = True

class HorarioUpdate(BaseModel):
    hora_inicio: Optional[str] = None
    hora_fin: Optional[str] = None
    activo: Optional[bool] = None

class FechaBloqueadaCreate(BaseModel):
    fecha: str  # "2026-06-15"
    hora_inicio: Optional[str] = None
    hora_fin: Optional[str] = None
    motivo: Optional[str] = None

class ReservaCreate(BaseModel):
    fecha: str
    hora_inicio: str
    hora_fin: str
    cantidad_ninos: int
    nombre_festejado: Optional[str] = None
    cliente_nombre: str
    cliente_email: str
    cliente_telefono: Optional[str] = None
    menu_seleccionado: list = []
    juegos_seleccionados: list = []
    precio_salon: float
    precio_menu: float
    precio_juegos: float
    precio_total: float
    observaciones: Optional[str] = None

class ReservaUpdate(BaseModel):
    estado: Optional[str] = None
    seña_pagada: Optional[bool] = None
    total_pagado: Optional[bool] = None
    observaciones: Optional[str] = None

class ConfiguracionUpdate(BaseModel):
    nombre_visible: Optional[str] = None
    color_primario: Optional[str] = None
    color_secundario: Optional[str] = None
    logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
    imagen_portada_url: Optional[str] = None
    direccion: Optional[str] = None
    whatsapp: Optional[str] = None
    email_contacto: Optional[str] = None
    capacidad_maxima: Optional[int] = None
    ninos_base: Optional[int] = None
    precio_base_salon: Optional[float] = None
    modalidad_cobro: Optional[str] = None
    porcentaje_seña: Optional[int] = None
    alias_transferencia: Optional[str] = None
    mensaje_pago: Optional[str] = None
    politica_cancelacion: Optional[str] = None

class RegistroPublico(BaseModel):
    nombre_salon: str
    email_admin: str
    whatsapp: str
    nombre_responsable: Optional[str] = None

class SuscripcionCreate(BaseModel):
    tenant_id: str
    monto_usd: float
    fecha_pago: str
    periodo_desde: str
    periodo_hasta: str
    metodo: Optional[str] = None
    referencia: Optional[str] = None
    notas: Optional[str] = None

class TenantCreate(BaseModel):
    nombre: str
    slug: str
    email_admin: str
    password: str
    nombre_visible: Optional[str] = None

# ============================================================
# ENDPOINTS PÚBLICOS
# ============================================================

@qfa_app.get("/health")
async def health():
    return {"status": "ok", "app": "QueFiestaApp"}


@qfa_app.post("/registro")
async def registro_publico(data: RegistroPublico):
    """Alta pública desde la landing — genera slug, contraseña temporal y envía email de bienvenida."""
    import re, secrets, string
    db = await get_qfa_db()
    try:
        # Generar slug desde el nombre del salón — sin guiones, todo minúsculas, sin caracteres especiales
        import unicodedata
        nombre_norm = unicodedata.normalize('NFD', data.nombre_salon.lower().strip())
        nombre_ascii = ''.join(c for c in nombre_norm if unicodedata.category(c) != 'Mn')
        slug_base = re.sub(r'[^a-z0-9]', '', nombre_ascii)
        slug_base = slug_base[:40] or 'mi-organizacion'

        # Verificar unicidad del slug — solo entre tenants activos
        slug = slug_base
        contador = 2
        while True:
            existe = await db.fetchrow(
                "SELECT id FROM qfa_tenants WHERE slug = $1 AND activo = TRUE", slug
            )
            if not existe:
                break
            slug = f"{slug_base}{contador}"
            contador += 1

        # Generar contraseña temporal
        alphabet = string.ascii_letters + string.digits
        password_temp = ''.join(secrets.choice(alphabet) for _ in range(10))
        password_hash = hash_password(password_temp)

        # Calcular trial
        trial_hasta = date.today() + timedelta(days=30)

        # Insertar tenant — un mismo email puede tener múltiples salones
        tenant = await db.fetchrow("""
            INSERT INTO qfa_tenants (nombre, slug, email_admin, password_hash, nombre_visible,
                                     trial_hasta, suscripcion_activa, whatsapp)
            VALUES ($1, $2, $3, $4, $5, $6, FALSE, $7)
            RETURNING id, slug, nombre, nombre_visible
        """, data.nombre_salon, slug, data.email_admin, password_hash,
            data.nombre_salon, trial_hasta, data.whatsapp)

        # Email de bienvenida con datos de acceso
        try:
            email_bienvenida_tenant_qfa(
                nombre_salon=data.nombre_salon,
                email_admin=data.email_admin,
                slug=slug,
                password=password_temp,
                trial_hasta=str(trial_hasta)
            )
        except Exception as e:
            print(f"[QFA EMAIL BIENVENIDA] Error: {e}")

        # Notificación a Claudia
        try:
            enviar_email_qfa(
                "pagos@gestionateia.com",
                f"🎉 Nuevo registro en QueFiestaApp — {data.nombre_salon}",
                f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
                    <div style="background:#1a1a2e;padding:24px;border-radius:12px 12px 0 0;">
                        <h2 style="color:#ff6b6b;margin:0;">🎉 Nuevo salón registrado</h2>
                    </div>
                    <div style="background:#f9f9fb;padding:24px;border-radius:0 0 12px 12px;">
                        <table style="border-collapse:collapse;width:100%;background:white;border-radius:8px;overflow:hidden;">
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;width:140px;">Salón</td><td style="padding:10px 14px;">{data.nombre_salon}</td></tr>
                            <tr><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Slug</td><td style="padding:10px 14px;font-family:monospace;">{slug}</td></tr>
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Email</td><td style="padding:10px 14px;">{data.email_admin}</td></tr>
                            <tr><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">WhatsApp</td><td style="padding:10px 14px;">{data.whatsapp or '—'}</td></tr>
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Trial hasta</td><td style="padding:10px 14px;">{trial_hasta}</td></tr>
                        </table>
                        <div style="margin-top:20px;text-align:center;">
                            <a href="https://quefiestaapp.gestionateia.com/superadmin.html"
                               style="background:#ff6b6b;color:white;padding:12px 24px;border-radius:50px;text-decoration:none;font-weight:bold;">
                                Ver en Superadmin →
                            </a>
                        </div>
                    </div>
                </div>
                """
            )
        except Exception as e:
            print(f"[QFA EMAIL NOTIF CLAUDIA] Error: {e}")

        return {
            "ok": True,
            "slug": slug,
            "trial_hasta": str(trial_hasta),
            "mensaje": f"¡Bienvenido! Tu cuenta fue creada. Revisá tu email para los datos de acceso."
        }
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Ese nombre de organización ya está registrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        release_db(db)


@qfa_app.get("/{slug}")
async def get_salon_publico(slug: str):
    """Datos públicos del salón para la página de reservas."""
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("""
            SELECT id, nombre_visible, nombre, color_primario, color_secundario,
                   logo_url, favicon_url, imagen_portada_url, imagenes_galeria,
                   direccion, whatsapp, email_contacto, redes_sociales,
                   ninos_base, precio_base_salon, capacidad_maxima,
                   modalidad_cobro, porcentaje_seña, alias_transferencia, mensaje_pago, politica_cancelacion,
                   suscripcion_activa, trial_hasta
            FROM qfa_tenants
            WHERE slug = $1 AND activo = TRUE
        """, slug)

        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        t = dict(tenant)

        # Verificar que la suscripción esté activa o en trial
        hoy = date.today()
        trial_ok = t["trial_hasta"] and t["trial_hasta"] >= hoy
        if not t["suscripcion_activa"] and not trial_ok:
            raise HTTPException(status_code=403, detail="Suscripción inactiva")

        # Parsear JSONB
        t["imagenes_galeria"] = json.loads(t["imagenes_galeria"]) if isinstance(t["imagenes_galeria"], str) else (t["imagenes_galeria"] or [])
        t["redes_sociales"] = json.loads(t["redes_sociales"]) if isinstance(t["redes_sociales"], str) else (t["redes_sociales"] or {})

        # Convertir fechas a string
        if t["trial_hasta"]:
            t["trial_hasta"] = str(t["trial_hasta"])

        return t
    finally:
        release_db(db)


@qfa_app.get("/{slug}/menu")
async def get_menu_publico(slug: str):
    """Ítems del menú activos para mostrar al cliente."""
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("SELECT id FROM qfa_tenants WHERE slug = $1 AND activo = TRUE", slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        items = await db.fetch("""
            SELECT id, nombre, descripcion, precio_base, precio_por_nino_extra, imagen_url, orden
            FROM qfa_menu_items
            WHERE tenant_id = $1 AND activo = TRUE
            ORDER BY orden ASC, nombre ASC
        """, tenant["id"])

        return [dict(i) for i in items]
    finally:
        release_db(db)


@qfa_app.get("/{slug}/juegos")
async def get_juegos_publico(slug: str):
    """Juegos y adicionales activos para mostrar al cliente."""
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("SELECT id FROM qfa_tenants WHERE slug = $1 AND activo = TRUE", slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        juegos = await db.fetch("""
            SELECT id, nombre, descripcion, precio_fijo, precio_por_nino, imagen_url, orden
            FROM qfa_juegos
            WHERE tenant_id = $1 AND activo = TRUE
            ORDER BY orden ASC, nombre ASC
        """, tenant["id"])

        return [dict(j) for j in juegos]
    finally:
        release_db(db)


@qfa_app.get("/{slug}/disponibilidad")
async def get_disponibilidad(slug: str, mes: int = None, anio: int = None):
    """
    Devuelve para cada día del mes:
    - si está disponible, bloqueado, o reservado
    - los horarios disponibles para ese día
    """
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("SELECT id FROM qfa_tenants WHERE slug = $1 AND activo = TRUE", slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        hoy = date.today()
        mes_consulta = mes or hoy.month
        anio_consulta = anio or hoy.year

        tenant_id = tenant["id"]

        # Horarios configurados por día de semana
        horarios = await db.fetch("""
            SELECT dia_semana, hora_inicio, hora_fin
            FROM qfa_horarios
            WHERE tenant_id = $1 AND activo = TRUE
            ORDER BY dia_semana, hora_inicio
        """, tenant_id)

        # Fechas bloqueadas en el mes
        fechas_bloqueadas = await db.fetch("""
            SELECT fecha, hora_inicio, hora_fin
            FROM qfa_fechas_bloqueadas
            WHERE tenant_id = $1
            AND EXTRACT(MONTH FROM fecha) = $2
            AND EXTRACT(YEAR FROM fecha) = $3
        """, tenant_id, mes_consulta, anio_consulta)

        # Reservas confirmadas en el mes
        reservas = await db.fetch("""
            SELECT fecha, hora_inicio, hora_fin
            FROM qfa_reservas
            WHERE tenant_id = $1
            AND EXTRACT(MONTH FROM fecha) = $2
            AND EXTRACT(YEAR FROM fecha) = $3
            AND estado IN ('pendiente', 'confirmada')
        """, tenant_id, mes_consulta, anio_consulta)

        # Construir mapa de disponibilidad
        horarios_por_dia = {}
        for h in horarios:
            dia = h["dia_semana"]
            if dia not in horarios_por_dia:
                horarios_por_dia[dia] = []
            horarios_por_dia[dia].append({
                "hora_inicio": str(h["hora_inicio"]),
                "hora_fin": str(h["hora_fin"])
            })

        bloqueadas_set = set()
        for fb in fechas_bloqueadas:
            if fb["hora_inicio"] is None:  # día entero bloqueado
                bloqueadas_set.add(str(fb["fecha"]))

        reservadas = {}
        for r in reservas:
            fecha_str = str(r["fecha"])
            if fecha_str not in reservadas:
                reservadas[fecha_str] = []
            reservadas[fecha_str].append({
                "hora_inicio": str(r["hora_inicio"]),
                "hora_fin": str(r["hora_fin"])
            })

        # Generar días del mes
        import calendar
        dias_en_mes = calendar.monthrange(anio_consulta, mes_consulta)[1]
        resultado = []

        for dia_num in range(1, dias_en_mes + 1):
            fecha = date(anio_consulta, mes_consulta, dia_num)
            fecha_str = str(fecha)
            dia_semana = fecha.weekday()  # 0=lunes en Python, convertir: lunes=1, domingo=0
            dia_semana_js = (dia_semana + 1) % 7  # convertir a formato JS (0=domingo)

            horarios_dia = horarios_por_dia.get(dia_semana_js, [])

            if fecha < hoy:
                estado = "pasado"
            elif fecha_str in bloqueadas_set:
                estado = "bloqueado"
            elif not horarios_dia:
                estado = "no_disponible"
            else:
                # Verificar si todos los horarios están reservados
                reservas_dia = reservadas.get(fecha_str, [])
                horarios_libres = []
                for h in horarios_dia:
                    ocupado = any(
                        r["hora_inicio"] == h["hora_inicio"]
                        for r in reservas_dia
                    )
                    if not ocupado:
                        horarios_libres.append(h)

                if horarios_libres:
                    estado = "disponible"
                else:
                    estado = "completo"

            resultado.append({
                "fecha": fecha_str,
                "dia": dia_num,
                "dia_semana": dia_semana_js,
                "estado": estado,
                "horarios": horarios_dia if estado == "disponible" else []
            })

        return {
            "mes": mes_consulta,
            "anio": anio_consulta,
            "dias": resultado
        }
    finally:
        release_db(db)


@qfa_app.post("/{slug}/reservas")
async def crear_reserva(slug: str, data: ReservaCreate):
    """El cliente envía su solicitud de reserva con el presupuesto armado."""
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("""
            SELECT id, nombre_visible, nombre, modalidad_cobro, porcentaje_seña,
                   email_contacto, ninos_base, precio_base_salon,
                   alias_transferencia, mensaje_pago, whatsapp
            FROM qfa_tenants
            WHERE slug = $1 AND activo = TRUE
        """, slug)

        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        tenant_id = tenant["id"]

        # Verificar que la fecha/horario no esté ya reservado
        existente = await db.fetchrow("""
            SELECT id FROM qfa_reservas
            WHERE tenant_id = $1
            AND fecha = $2
            AND hora_inicio = $3
            AND estado IN ('pendiente', 'confirmada')
        """, tenant_id, data.fecha, data.hora_inicio)

        if existente:
            raise HTTPException(status_code=409, detail="Ese horario ya está reservado")

        # Calcular seña si aplica
        monto_seña = 0.0
        if tenant["modalidad_cobro"] in ("seña_transferencia_resto_efectivo", "seña_transferencia_resto_transferencia"):
            monto_seña = round(data.precio_total * (tenant["porcentaje_seña"] or 30) / 100, 2)

        # Crear la reserva
        reserva = await db.fetchrow("""
            INSERT INTO qfa_reservas (
                tenant_id, fecha, hora_inicio, hora_fin,
                cantidad_ninos, nombre_festejado,
                cliente_nombre, cliente_email, cliente_telefono,
                menu_seleccionado, juegos_seleccionados,
                precio_salon, precio_menu, precio_juegos, precio_total,
                modalidad_cobro, monto_seña,
                observaciones, origen
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6,
                $7, $8, $9,
                $10, $11,
                $12, $13, $14, $15,
                $16, $17,
                $18, 'web'
            )
            RETURNING id, created_at
        """,
            tenant_id,
            data.fecha, data.hora_inicio, data.hora_fin,
            data.cantidad_ninos, data.nombre_festejado,
            data.cliente_nombre, data.cliente_email, data.cliente_telefono,
            json.dumps(data.menu_seleccionado), json.dumps(data.juegos_seleccionados),
            data.precio_salon, data.precio_menu, data.precio_juegos, data.precio_total,
            tenant["modalidad_cobro"], monto_seña,
            data.observaciones
        )

        # Enviar email de confirmación al cliente
        try:
            email_confirmacion_reserva(
                cliente_nombre=data.cliente_nombre,
                cliente_email=data.cliente_email,
                salon_nombre=tenant["nombre_visible"] or tenant["nombre"],
                fecha=data.fecha,
                hora_inicio=data.hora_inicio,
                hora_fin=data.hora_fin,
                cantidad_ninos=data.cantidad_ninos,
                nombre_festejado=data.nombre_festejado or "",
                menu_seleccionado=data.menu_seleccionado,
                juegos_seleccionados=data.juegos_seleccionados,
                precio_total=data.precio_total,
                monto_seña=monto_seña,
                modalidad_cobro=tenant["modalidad_cobro"],
                alias_transferencia=tenant["alias_transferencia"] or "",
                mensaje_pago=tenant["mensaje_pago"] or "",
                whatsapp_salon=tenant["whatsapp"] or ""
            )
        except Exception as e:
            print(f"[QFA EMAIL RESERVA] Error: {e}")

        return {
            "ok": True,
            "reserva_id": str(reserva["id"]),
            "mensaje": "Tu solicitud fue recibida. Te contactaremos para confirmar.",
            "monto_seña": monto_seña,
            "modalidad_cobro": tenant["modalidad_cobro"]
        }
    finally:
        release_db(db)


# ============================================================
# ENDPOINTS ADMIN
# ============================================================

@qfa_app.post("/admin/login")
async def admin_login(data: LoginRequest):
    """Login del dueño del salón."""
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("""
            SELECT id, slug, nombre_visible, nombre, password_hash, suscripcion_activa, trial_hasta
            FROM qfa_tenants
            WHERE slug = $1 AND activo = TRUE
        """, data.slug)

        if not tenant:
            raise HTTPException(status_code=401, detail="Salón no encontrado")

        # Verificar suscripción
        hoy = date.today()
        trial_ok = tenant["trial_hasta"] and tenant["trial_hasta"] >= hoy
        if not tenant["suscripcion_activa"] and not trial_ok:
            raise HTTPException(status_code=403, detail="Suscripción inactiva. Contactá a GestionaTeIA.")

        # Verificar contraseña — primero usuario principal, luego usuarios adicionales
        if verify_password(data.password, tenant["password_hash"]):
            # Login con usuario principal (slug + password)
            pass
        else:
            # Buscar en usuarios adicionales por email = data.slug (campo reutilizado como email)
            usuario_adicional = await db.fetchrow("""
                SELECT id, password_hash FROM qfa_admin_usuarios
                WHERE tenant_id = $1 AND email = $2 AND activo = TRUE
            """, tenant["id"], data.slug)
            if not usuario_adicional or not verify_password(data.password, usuario_adicional["password_hash"]):
                raise HTTPException(status_code=401, detail="Credenciales incorrectas")

        token = create_token(str(tenant["id"]), tenant["slug"])

        return {
            "token": token,
            "slug": tenant["slug"],
            "nombre": tenant["nombre_visible"] or tenant["nombre"]
        }
    finally:
        release_db(db)


# --- MENÚ ---

@qfa_app.get("/admin/{slug}/menu")
async def admin_get_menu(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        items = await db.fetch("""
            SELECT * FROM qfa_menu_items
            WHERE tenant_id = $1
            ORDER BY orden ASC, nombre ASC
        """, auth["tenant_id"])
        return [dict(i) for i in items]
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/menu")
async def admin_crear_menu_item(slug: str, data: MenuItemCreate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        item = await db.fetchrow("""
            INSERT INTO qfa_menu_items
                (tenant_id, nombre, descripcion, precio_base, precio_por_nino_extra, imagen_url, activo, orden)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """, auth["tenant_id"], data.nombre, data.descripcion,
            data.precio_base, data.precio_por_nino_extra,
            data.imagen_url, data.activo, data.orden)
        return dict(item)
    finally:
        release_db(db)


@qfa_app.put("/admin/{slug}/menu/{item_id}")
async def admin_actualizar_menu_item(slug: str, item_id: str, data: MenuItemUpdate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        campos = {k: v for k, v in data.dict().items() if v is not None}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos para actualizar")

        sets = ", ".join([f"{k} = ${i+3}" for i, k in enumerate(campos.keys())])
        valores = list(campos.values())

        item = await db.fetchrow(f"""
            UPDATE qfa_menu_items
            SET {sets}, updated_at = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING *
        """, item_id, auth["tenant_id"], *valores)

        if not item:
            raise HTTPException(status_code=404, detail="Ítem no encontrado")
        return dict(item)
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/menu/{item_id}")
async def admin_eliminar_menu_item(slug: str, item_id: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("""
            DELETE FROM qfa_menu_items WHERE id = $1 AND tenant_id = $2
        """, item_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


# --- JUEGOS ---

@qfa_app.get("/admin/{slug}/juegos")
async def admin_get_juegos(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        juegos = await db.fetch("""
            SELECT * FROM qfa_juegos
            WHERE tenant_id = $1
            ORDER BY orden ASC, nombre ASC
        """, auth["tenant_id"])
        return [dict(j) for j in juegos]
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/juegos")
async def admin_crear_juego(slug: str, data: JuegoCreate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        juego = await db.fetchrow("""
            INSERT INTO qfa_juegos
                (tenant_id, nombre, descripcion, precio_fijo, precio_por_nino, imagen_url, activo, orden)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
        """, auth["tenant_id"], data.nombre, data.descripcion,
            data.precio_fijo, data.precio_por_nino,
            data.imagen_url, data.activo, data.orden)
        return dict(juego)
    finally:
        release_db(db)


@qfa_app.put("/admin/{slug}/juegos/{juego_id}")
async def admin_actualizar_juego(slug: str, juego_id: str, data: JuegoUpdate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        campos = {k: v for k, v in data.dict().items() if v is not None}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos para actualizar")

        sets = ", ".join([f"{k} = ${i+3}" for i, k in enumerate(campos.keys())])
        valores = list(campos.values())

        juego = await db.fetchrow(f"""
            UPDATE qfa_juegos
            SET {sets}, updated_at = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING *
        """, juego_id, auth["tenant_id"], *valores)

        if not juego:
            raise HTTPException(status_code=404, detail="Juego no encontrado")
        return dict(juego)
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/juegos/{juego_id}")
async def admin_eliminar_juego(slug: str, juego_id: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("""
            DELETE FROM qfa_juegos WHERE id = $1 AND tenant_id = $2
        """, juego_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


# --- HORARIOS ---

@qfa_app.get("/admin/{slug}/horarios")
async def admin_get_horarios(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        horarios = await db.fetch("""
            SELECT id, dia_semana, hora_inicio::text, hora_fin::text, activo
            FROM qfa_horarios
            WHERE tenant_id = $1
            ORDER BY dia_semana, hora_inicio
        """, auth["tenant_id"])
        return [dict(h) for h in horarios]
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/horarios")
async def admin_crear_horario(slug: str, data: HorarioCreate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        horario = await db.fetchrow("""
            INSERT INTO qfa_horarios (tenant_id, dia_semana, hora_inicio, hora_fin, activo)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, dia_semana, hora_inicio::text, hora_fin::text, activo
        """, auth["tenant_id"], data.dia_semana, data.hora_inicio, data.hora_fin, data.activo)
        return dict(horario)
    finally:
        release_db(db)


@qfa_app.put("/admin/{slug}/horarios/{horario_id}")
async def admin_actualizar_horario(slug: str, horario_id: str, data: HorarioUpdate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        campos = {k: v for k, v in data.dict().items() if v is not None}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos para actualizar")
        sets = ", ".join([f"{k} = ${i+3}" for i, k in enumerate(campos.keys())])
        horario = await db.fetchrow(f"""
            UPDATE qfa_horarios SET {sets}
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, dia_semana, hora_inicio::text, hora_fin::text, activo
        """, horario_id, auth["tenant_id"], *list(campos.values()))
        if not horario:
            raise HTTPException(status_code=404, detail="Horario no encontrado")
        return dict(horario)
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/horarios/{horario_id}")
async def admin_eliminar_horario(slug: str, horario_id: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("DELETE FROM qfa_horarios WHERE id = $1 AND tenant_id = $2", horario_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


# --- FECHAS BLOQUEADAS ---

@qfa_app.get("/admin/{slug}/fechas-bloqueadas")
async def admin_get_fechas_bloqueadas(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        fechas = await db.fetch("""
            SELECT id, fecha::text, hora_inicio::text, hora_fin::text, motivo
            FROM qfa_fechas_bloqueadas
            WHERE tenant_id = $1
            ORDER BY fecha ASC
        """, auth["tenant_id"])
        return [dict(f) for f in fechas]
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/fechas-bloqueadas")
async def admin_crear_fecha_bloqueada(slug: str, data: FechaBloqueadaCreate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        fecha = await db.fetchrow("""
            INSERT INTO qfa_fechas_bloqueadas (tenant_id, fecha, hora_inicio, hora_fin, motivo)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, fecha::text, hora_inicio::text, hora_fin::text, motivo
        """, auth["tenant_id"], data.fecha, data.hora_inicio, data.hora_fin, data.motivo)
        return dict(fecha)
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/fechas-bloqueadas/{fecha_id}")
async def admin_eliminar_fecha_bloqueada(slug: str, fecha_id: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("DELETE FROM qfa_fechas_bloqueadas WHERE id = $1 AND tenant_id = $2", fecha_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


# --- RESERVAS ---

@qfa_app.get("/admin/{slug}/reservas")
async def admin_get_reservas(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        reservas = await db.fetch("""
            SELECT id, fecha::text, hora_inicio::text, hora_fin::text,
                   cantidad_ninos, nombre_festejado,
                   cliente_nombre, cliente_email, cliente_telefono,
                   precio_salon, precio_menu, precio_juegos, precio_total,
                   menu_seleccionado, juegos_seleccionados,
                   modalidad_cobro, monto_seña, seña_pagada, total_pagado,
                   estado, origen, observaciones,
                   created_at::text
            FROM qfa_reservas
            WHERE tenant_id = $1
            ORDER BY fecha DESC, hora_inicio DESC
        """, auth["tenant_id"])

        result = []
        for r in reservas:
            row = dict(r)
            row["menu_seleccionado"] = json.loads(row["menu_seleccionado"]) if isinstance(row["menu_seleccionado"], str) else (row["menu_seleccionado"] or [])
            row["juegos_seleccionados"] = json.loads(row["juegos_seleccionados"]) if isinstance(row["juegos_seleccionados"], str) else (row["juegos_seleccionados"] or [])
            result.append(row)

        return result
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/reservas")
async def admin_crear_reserva_manual(slug: str, data: ReservaCreate, auth=Depends(get_admin_token)):
    """El admin carga una reserva manualmente (ej: llegó por WhatsApp)."""
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("SELECT modalidad_cobro, porcentaje_seña FROM qfa_tenants WHERE id = $1", auth["tenant_id"])
        monto_seña = 0.0
        if tenant["modalidad_cobro"] in ("seña_transferencia_resto_efectivo", "seña_transferencia_resto_transferencia"):
            monto_seña = round(data.precio_total * (tenant["porcentaje_seña"] or 30) / 100, 2)

        reserva = await db.fetchrow("""
            INSERT INTO qfa_reservas (
                tenant_id, fecha, hora_inicio, hora_fin,
                cantidad_ninos, nombre_festejado,
                cliente_nombre, cliente_email, cliente_telefono,
                menu_seleccionado, juegos_seleccionados,
                precio_salon, precio_menu, precio_juegos, precio_total,
                modalidad_cobro, monto_seña, observaciones, origen
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                $10, $11, $12, $13, $14, $15, $16, $17, $18, 'manual'
            )
            RETURNING id, created_at::text
        """,
            auth["tenant_id"],
            data.fecha, data.hora_inicio, data.hora_fin,
            data.cantidad_ninos, data.nombre_festejado,
            data.cliente_nombre, data.cliente_email, data.cliente_telefono,
            json.dumps(data.menu_seleccionado), json.dumps(data.juegos_seleccionados),
            data.precio_salon, data.precio_menu, data.precio_juegos, data.precio_total,
            tenant["modalidad_cobro"], monto_seña, data.observaciones
        )
        return {"ok": True, "reserva_id": str(reserva["id"])}
    finally:
        release_db(db)


@qfa_app.patch("/admin/{slug}/reservas/{reserva_id}")
async def admin_actualizar_reserva(slug: str, reserva_id: str, data: ReservaUpdate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        campos = {k: v for k, v in data.dict().items() if v is not None}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos para actualizar")
        sets = ", ".join([f"{k} = ${i+3}" for i, k in enumerate(campos.keys())])
        reserva = await db.fetchrow(f"""
            UPDATE qfa_reservas SET {sets}, updated_at = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING id, estado
        """, reserva_id, auth["tenant_id"], *list(campos.values()))
        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")
        return dict(reserva)
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/reservas/{reserva_id}")
async def admin_eliminar_reserva(slug: str, reserva_id: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("DELETE FROM qfa_reservas WHERE id = $1 AND tenant_id = $2", reserva_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


# --- CONFIGURACIÓN DEL SALÓN ---

@qfa_app.get("/admin/{slug}/configuracion")
async def admin_get_configuracion(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("""
            SELECT nombre, nombre_visible, color_primario, color_secundario,
                   logo_url, favicon_url, imagen_portada_url, imagenes_galeria,
                   direccion, whatsapp, email_contacto, redes_sociales,
                   capacidad_maxima, ninos_base, precio_base_salon,
                   modalidad_cobro, porcentaje_seña, alias_transferencia, mensaje_pago,
                   suscripcion_activa, trial_hasta::text
            FROM qfa_tenants WHERE id = $1
        """, auth["tenant_id"])
        t = dict(tenant)
        t["imagenes_galeria"] = json.loads(t["imagenes_galeria"]) if isinstance(t["imagenes_galeria"], str) else (t["imagenes_galeria"] or [])
        t["redes_sociales"] = json.loads(t["redes_sociales"]) if isinstance(t["redes_sociales"], str) else (t["redes_sociales"] or {})
        return t
    finally:
        release_db(db)


class AdminUsuarioCreate(BaseModel):
    nombre: str
    email: str
    password: str


@qfa_app.get("/admin/{slug}/usuarios")
async def admin_get_usuarios(slug: str, auth=Depends(get_admin_token)):
    """Lista los usuarios admin adicionales del salón."""
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        usuarios = await db.fetch("""
            SELECT id, nombre, email, activo, created_at::text
            FROM qfa_admin_usuarios
            WHERE tenant_id = $1
            ORDER BY created_at ASC
        """, auth["tenant_id"])
        return [dict(u) for u in usuarios]
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/usuarios")
async def admin_crear_usuario(slug: str, data: AdminUsuarioCreate, auth=Depends(get_admin_token)):
    """Crea un usuario admin adicional para el salón."""
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")

    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres")

    db = await get_qfa_db()
    try:
        password_hash = hash_password(data.password)
        usuario = await db.fetchrow("""
            INSERT INTO qfa_admin_usuarios (tenant_id, nombre, email, password_hash)
            VALUES ($1, $2, $3, $4)
            RETURNING id, nombre, email, activo, created_at::text
        """, auth["tenant_id"], data.nombre, data.email, password_hash)
        return dict(usuario)
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese email")
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/usuarios/{usuario_id}")
async def admin_eliminar_usuario(slug: str, usuario_id: str, auth=Depends(get_admin_token)):
    """Elimina un usuario admin adicional."""
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("""
            DELETE FROM qfa_admin_usuarios WHERE id = $1 AND tenant_id = $2
        """, usuario_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


class CambioPasswordRequest(BaseModel):
    password_actual: str
    password_nueva: str


@qfa_app.post("/admin/{slug}/cambiar-password")
async def admin_cambiar_password(slug: str, data: CambioPasswordRequest, auth=Depends(get_admin_token)):
    """El admin del salón cambia su propia contraseña."""
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")

    if len(data.password_nueva) < 8:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 8 caracteres")

    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow(
            "SELECT password_hash FROM qfa_tenants WHERE id = $1", auth["tenant_id"]
        )
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")

        if not verify_password(data.password_actual, tenant["password_hash"]):
            raise HTTPException(status_code=401, detail="La contraseña actual es incorrecta")

        nuevo_hash = hash_password(data.password_nueva)
        await db.execute(
            "UPDATE qfa_tenants SET password_hash = $1, updated_at = NOW() WHERE id = $2",
            nuevo_hash, auth["tenant_id"]
        )
        return {"ok": True, "mensaje": "Contraseña actualizada correctamente"}
    finally:
        release_db(db)


@qfa_app.patch("/admin/{slug}/configuracion")
async def admin_actualizar_configuracion(slug: str, data: ConfiguracionUpdate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        campos = {k: v for k, v in data.dict().items() if v is not None}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos para actualizar")
        sets = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(campos.keys())])
        await db.execute(f"""
            UPDATE qfa_tenants SET {sets}, updated_at = NOW()
            WHERE id = $1
        """, auth["tenant_id"], *list(campos.values()))
        return {"ok": True}
    finally:
        release_db(db)


# ============================================================
# SUBIDA DE COMPROBANTE DE PAGO (público — lo sube el cliente)
# ============================================================

@qfa_app.post("/{slug}/comprobante/{reserva_id}")
async def subir_comprobante(slug: str, reserva_id: str, file: UploadFile = File(...)):
    """El cliente sube el comprobante de transferencia al confirmar la reserva."""

    # Validar tipo de archivo (imagen o PDF)
    content_type = file.content_type or ""
    if not content_type.startswith("image/") and content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes o PDF")

    # Validar tamaño (máx 5MB)
    contenido = await file.read()
    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El archivo no puede superar 5MB")

    # Verificar que la reserva existe y pertenece al salón
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("SELECT id FROM qfa_tenants WHERE slug = $1 AND activo = TRUE", slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        reserva = await db.fetchrow(
            "SELECT id FROM qfa_reservas WHERE id = $1 AND tenant_id = $2",
            reserva_id, tenant["id"]
        )
        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")

        # Generar nombre único
        extension = file.filename.split(".")[-1] if "." in file.filename else "jpg"
        nombre_archivo = f"comprobantes/{slug}/{reserva_id}.{extension}"

        # Subir a Supabase Storage
        url_upload = f"{QFA_SUPABASE_URL}/storage/v1/object/qfa-imagenes/{nombre_archivo}"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url_upload,
                content=contenido,
                headers={
                    "Authorization": f"Bearer {QFA_SUPABASE_SERVICE_KEY}",
                    "Content-Type": content_type,
                }
            )

        if response.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail="Error al subir comprobante")

        # URL pública
        url_publica = f"{QFA_SUPABASE_URL}/storage/v1/object/public/qfa-imagenes/{nombre_archivo}"

        # Actualizar la reserva con la URL del comprobante
        await db.execute(
            "UPDATE qfa_reservas SET comprobante_url = $1, updated_at = NOW() WHERE id = $2",
            url_publica, reserva_id
        )

        return {"ok": True, "url": url_publica}

    finally:
        release_db(db)


# ============================================================
# COMBOS / OFERTAS
# ============================================================

class ComboCreate(BaseModel):
    nombre: str
    descripcion: Optional[str] = None
    precio_cerrado: float
    imagen_url: Optional[str] = None
    activo: bool = True
    orden: int = 0

class ComboUpdate(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    precio_cerrado: Optional[float] = None
    imagen_url: Optional[str] = None
    activo: Optional[bool] = None
    orden: Optional[int] = None


@qfa_app.get("/{slug}/combos")
async def get_combos_publico(slug: str):
    """Combos activos para mostrar al cliente antes del presupuestador."""
    db = await get_qfa_db()
    try:
        tenant = await db.fetchrow("SELECT id FROM qfa_tenants WHERE slug = $1 AND activo = TRUE", slug)
        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        combos = await db.fetch("""
            SELECT id, nombre, descripcion, precio_cerrado, imagen_url, orden
            FROM qfa_combos
            WHERE tenant_id = $1 AND activo = TRUE
            ORDER BY orden ASC, nombre ASC
        """, tenant["id"])

        return [dict(c) for c in combos]
    finally:
        release_db(db)


@qfa_app.get("/admin/{slug}/combos")
async def admin_get_combos(slug: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        combos = await db.fetch("""
            SELECT * FROM qfa_combos
            WHERE tenant_id = $1
            ORDER BY orden ASC, nombre ASC
        """, auth["tenant_id"])
        return [dict(c) for c in combos]
    finally:
        release_db(db)


@qfa_app.post("/admin/{slug}/combos")
async def admin_crear_combo(slug: str, data: ComboCreate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        combo = await db.fetchrow("""
            INSERT INTO qfa_combos
                (tenant_id, nombre, descripcion, precio_cerrado, imagen_url, activo, orden)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
        """, auth["tenant_id"], data.nombre, data.descripcion,
            data.precio_cerrado, data.imagen_url, data.activo, data.orden)
        return dict(combo)
    finally:
        release_db(db)


@qfa_app.put("/admin/{slug}/combos/{combo_id}")
async def admin_actualizar_combo(slug: str, combo_id: str, data: ComboUpdate, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        campos = {k: v for k, v in data.dict().items() if v is not None}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos para actualizar")
        sets = ", ".join([f"{k} = ${i+3}" for i, k in enumerate(campos.keys())])
        combo = await db.fetchrow(f"""
            UPDATE qfa_combos SET {sets}, updated_at = NOW()
            WHERE id = $1 AND tenant_id = $2
            RETURNING *
        """, combo_id, auth["tenant_id"], *list(campos.values()))
        if not combo:
            raise HTTPException(status_code=404, detail="Combo no encontrado")
        return dict(combo)
    finally:
        release_db(db)


@qfa_app.delete("/admin/{slug}/combos/{combo_id}")
async def admin_eliminar_combo(slug: str, combo_id: str, auth=Depends(get_admin_token)):
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")
    db = await get_qfa_db()
    try:
        await db.execute("DELETE FROM qfa_combos WHERE id = $1 AND tenant_id = $2", combo_id, auth["tenant_id"])
        return {"ok": True}
    finally:
        release_db(db)


# ============================================================
# SUBIDA DE IMÁGENES — Supabase Storage (bucket: qfa-imagenes)
# ============================================================

@qfa_app.post("/admin/{slug}/imagen")
async def subir_imagen(slug: str, file: UploadFile = File(...), auth=Depends(get_admin_token)):
    """Sube una imagen a Supabase Storage y devuelve la URL pública."""
    if auth["slug"] != slug:
        raise HTTPException(status_code=403, detail="Sin acceso")

    # Validar tipo de archivo
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Solo se permiten imágenes")

    # Validar tamaño (máx 5MB)
    contenido = await file.read()
    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="La imagen no puede superar 5MB")

    # Generar nombre único
    extension = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    nombre_archivo = f"{slug}/{uuid.uuid4()}.{extension}"

    # Subir a Supabase Storage
    url_upload = f"{QFA_SUPABASE_URL}/storage/v1/object/qfa-imagenes/{nombre_archivo}"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url_upload,
            content=contenido,
            headers={
                "Authorization": f"Bearer {QFA_SUPABASE_SERVICE_KEY}",
                "Content-Type": content_type,
            }
        )

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Error al subir imagen: {response.text}")

    # URL pública
    url_publica = f"{QFA_SUPABASE_URL}/storage/v1/object/public/qfa-imagenes/{nombre_archivo}"
    return {"url": url_publica}


# ============================================================
# ENDPOINTS SUPERADMIN (Claudia)
# ============================================================

@qfa_app.get("/superadmin/tenants")
async def superadmin_get_tenants(auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        tenants = await db.fetch("""
            SELECT id, nombre, slug, email_admin, nombre_visible,
                   suscripcion_activa, suscripcion_vence::text, trial_hasta::text,
                   activo, created_at::text
            FROM qfa_tenants
            ORDER BY created_at DESC
        """)
        return [dict(t) for t in tenants]
    finally:
        release_db(db)


@qfa_app.post("/superadmin/tenants")
async def superadmin_crear_tenant(data: TenantCreate, auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        password_hash = hash_password(data.password)
        trial_hasta = date.today() + timedelta(days=30)

        tenant = await db.fetchrow("""
            INSERT INTO qfa_tenants (nombre, slug, email_admin, password_hash, nombre_visible, trial_hasta)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, slug, nombre, nombre_visible
        """, data.nombre, data.slug, data.email_admin, password_hash,
            data.nombre_visible or data.nombre, trial_hasta)

        # Enviar email de bienvenida con datos de acceso
        try:
            email_bienvenida_tenant_qfa(
                nombre_salon=data.nombre_visible or data.nombre,
                email_admin=data.email_admin,
                slug=data.slug,
                password=data.password,
                trial_hasta=str(trial_hasta)
            )
        except Exception as e:
            print(f"[QFA EMAIL BIENVENIDA] Error: {e}")

        return {"ok": True, "tenant": dict(tenant)}
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="El slug o email ya existe")
    finally:
        release_db(db)


@qfa_app.patch("/superadmin/tenants/{tenant_id}")
async def superadmin_actualizar_tenant(tenant_id: str, data: dict, auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        campos_permitidos = {"suscripcion_activa", "suscripcion_vence", "trial_hasta", "activo"}
        campos = {k: v for k, v in data.items() if k in campos_permitidos}
        if not campos:
            raise HTTPException(status_code=400, detail="No hay campos válidos para actualizar")
        sets = ", ".join([f"{k} = ${i+2}" for i, k in enumerate(campos.keys())])
        await db.execute(f"""
            UPDATE qfa_tenants SET {sets}, updated_at = NOW() WHERE id = $1
        """, tenant_id, *list(campos.values()))
        return {"ok": True}
    finally:
        release_db(db)


@qfa_app.get("/superadmin/suscripciones")
async def superadmin_get_suscripciones(auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        rows = await db.fetch("""
            SELECT s.*, t.nombre as tenant_nombre
            FROM qfa_suscripciones s
            JOIN qfa_tenants t ON s.tenant_id = t.id
            ORDER BY s.fecha_pago DESC
        """)
        return [dict(r) for r in rows]
    finally:
        release_db(db)


@qfa_app.post("/superadmin/suscripciones")
async def superadmin_registrar_suscripcion(data: SuscripcionCreate, auth=Depends(get_superadmin_token)):
    from uuid import UUID
    from datetime import date as date_type
    db = await get_qfa_db()
    try:
        tenant_uuid = UUID(data.tenant_id)
        fecha_pago = date_type.fromisoformat(data.fecha_pago)
        periodo_desde = date_type.fromisoformat(data.periodo_desde)
        periodo_hasta = date_type.fromisoformat(data.periodo_hasta)

        sus = await db.fetchrow("""
            INSERT INTO qfa_suscripciones
                (tenant_id, monto_usd, fecha_pago, periodo_desde, periodo_hasta, metodo, referencia, notas)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        """, tenant_uuid, data.monto_usd, fecha_pago,
            periodo_desde, periodo_hasta,
            data.metodo, data.referencia, data.notas)

        # Activar suscripción del tenant
        await db.execute("""
            UPDATE qfa_tenants
            SET suscripcion_activa = TRUE, suscripcion_vence = $2, updated_at = NOW()
            WHERE id = $1
        """, tenant_uuid, periodo_hasta)

        # Obtener nombre del tenant para el email
        tenant_row = await db.fetchrow(
            "SELECT nombre_visible, nombre, email_admin FROM qfa_tenants WHERE id = $1", tenant_uuid
        )
        nombre_salon = (tenant_row["nombre_visible"] or tenant_row["nombre"]) if tenant_row else data.tenant_id
        email_salon = tenant_row["email_admin"] if tenant_row else "—"

        # Notificación a Claudia
        try:
            enviar_email_qfa(
                "pagos@gestionateia.com",
                f"💰 Pago de suscripción registrado — {nombre_salon}",
                f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
                    <div style="background:#1a1a2e;padding:24px;border-radius:12px 12px 0 0;">
                        <h2 style="color:#71D997;margin:0;">💰 Nuevo pago de suscripción</h2>
                    </div>
                    <div style="background:#f9f9fb;padding:24px;border-radius:0 0 12px 12px;">
                        <table style="border-collapse:collapse;width:100%;background:white;border-radius:8px;overflow:hidden;">
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;width:140px;">Salón</td><td style="padding:10px 14px;">{nombre_salon}</td></tr>
                            <tr><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Email</td><td style="padding:10px 14px;">{email_salon}</td></tr>
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Monto</td><td style="padding:10px 14px;font-weight:bold;color:#71D997;">USD {data.monto_usd:.2f}</td></tr>
                            <tr><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Método</td><td style="padding:10px 14px;">{data.metodo or '—'}</td></tr>
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Fecha de pago</td><td style="padding:10px 14px;">{data.fecha_pago}</td></tr>
                            <tr><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Período</td><td style="padding:10px 14px;">{data.periodo_desde} → {data.periodo_hasta}</td></tr>
                            <tr style="background:#f0f4f8"><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Referencia</td><td style="padding:10px 14px;">{data.referencia or '—'}</td></tr>
                            <tr><td style="padding:10px 14px;font-weight:bold;color:#2C3E50;">Notas</td><td style="padding:10px 14px;">{data.notas or '—'}</td></tr>
                        </table>
                        <div style="margin-top:20px;text-align:center;">
                            <a href="https://quefiestaapp.gestionateia.com/superadmin.html"
                               style="background:#71D997;color:#0f2010;padding:12px 24px;border-radius:50px;text-decoration:none;font-weight:bold;">
                                Ver en Superadmin →
                            </a>
                        </div>
                    </div>
                </div>
                """
            )
        except Exception as e:
            print(f"[QFA EMAIL NOTIF SUSCRIPCION] Error: {e}")

        return {"ok": True, "suscripcion_id": str(sus["id"])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        release_db(db)


@qfa_app.delete("/superadmin/tenants/{tenant_id}")
async def superadmin_eliminar_tenant(tenant_id: str, auth=Depends(get_superadmin_token)):
    """Elimina un tenant y todos sus datos en cascada (irreversible)."""
    db = await get_qfa_db()
    try:
        # Verificar que existe
        tenant = await db.fetchrow("SELECT id, nombre FROM qfa_tenants WHERE id = $1", tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Salón no encontrado")

        # Eliminar — CASCADE borra automáticamente: menu_items, juegos, horarios,
        # fechas_bloqueadas, reservas, pagos, suscripciones, combos
        await db.execute("DELETE FROM qfa_tenants WHERE id = $1", tenant_id)

        return {"ok": True, "eliminado": tenant["nombre"]}
    finally:
        release_db(db)


@qfa_app.get("/superadmin/stats")
async def superadmin_stats(auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        total = await db.fetchval("SELECT COUNT(*) FROM qfa_tenants")
        activos = await db.fetchval("SELECT COUNT(*) FROM qfa_tenants WHERE suscripcion_activa = TRUE")
        en_trial = await db.fetchval("SELECT COUNT(*) FROM qfa_tenants WHERE trial_hasta >= CURRENT_DATE AND suscripcion_activa = FALSE")
        total_reservas = await db.fetchval("SELECT COUNT(*) FROM qfa_reservas")
        return {
            "total_salones": total,
            "suscriptos": activos,
            "en_trial": en_trial,
            "total_reservas": total_reservas
        }
    finally:
        release_db(db)


@qfa_app.get("/superadmin/configuracion")
async def superadmin_get_configuracion(auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        rows = await db.fetch("SELECT clave, valor FROM qfa_configuracion_global")
        config = {r["clave"]: r["valor"] for r in rows}
        return {
            "precio_mensual_usd": float(config.get("precio_mensual_usd", 25)),
            "valor_dolar_oficial": float(config.get("valor_dolar_oficial", 1415)),
            "dias_trial": int(config.get("dias_trial", 30)),
        }
    finally:
        release_db(db)


@qfa_app.post("/superadmin/configuracion")
async def superadmin_guardar_configuracion(data: dict, auth=Depends(get_superadmin_token)):
    db = await get_qfa_db()
    try:
        campos = {
            "precio_mensual_usd": data.get("precio_mensual_usd"),
            "valor_dolar_oficial": data.get("valor_dolar_oficial"),
            "dias_trial": data.get("dias_trial"),
        }
        for clave, valor in campos.items():
            if valor is not None:
                await db.execute("""
                    INSERT INTO qfa_configuracion_global (clave, valor)
                    VALUES ($1, $2)
                    ON CONFLICT (clave) DO UPDATE SET valor = $2, updated_at = NOW()
                """, clave, str(valor))
        # Cambiar contraseña superadmin si viene
        if data.get("superadmin_password"):
            await db.execute("""
                INSERT INTO qfa_configuracion_global (clave, valor)
                VALUES ('superadmin_password', $1)
                ON CONFLICT (clave) DO UPDATE SET valor = $1, updated_at = NOW()
            """, data["superadmin_password"])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        release_db(db)
