from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import date, time, datetime
from typing import Optional
import asyncpg
import os
import resend
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
import openpyxl
from io import BytesIO
import bcrypt

load_dotenv()

app = FastAPI(title="Sistema de Reserva de Espacios")

resend.api_key = os.getenv("RESEND_API_KEY")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

pool = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        min_size=1,
        max_size=5
    )

@app.on_event("shutdown")
async def shutdown():
    await pool.close()

async def get_db():
    return await pool.acquire()

async def release_db(conn):
    await pool.release(conn)

# ===== MULTI-TENANT =====
async def get_tenant(request: Request, db):
    host = request.headers.get("host", "")
    # Extraer subdominio: prueba.reservas.com -> prueba
    # En desarrollo local usamos header X-Tenant-Slug
    slug = request.headers.get("X-Tenant-Slug", "")
    if not slug:
        parts = host.split(".")
        if len(parts) >= 3:
            slug = parts[0]
        else:
            # Fallback para desarrollo: usar tenant de prueba
            slug = "prueba"

    tenant = await db.fetchrow(
        "SELECT * FROM tenants WHERE slug = $1 AND activo = TRUE",
        slug
    )
    if not tenant:
        raise HTTPException(status_code=404, detail="Organización no encontrada")

    # Verificar si el trial venció y no tiene suscripción activa
    if not tenant["suscripcion_activa"] and tenant["trial_hasta"] < date.today():
        raise HTTPException(status_code=402, detail="El período de prueba ha vencido. Por favor suscribite para continuar.")

    return dict(tenant)

# ===== EMAIL =====
def enviar_email(destinatario: str, asunto: str, cuerpo: str):
    try:
        resend.Emails.send({
            "from": "ReservaSpace <hola@reservatuespacio.com>",
            "to": destinatario,
            "subject": asunto,
            "html": cuerpo
        })
    except Exception as e:
        print(f"Error al enviar email: {e}")

# Modelos
class ReservaCreate(BaseModel):
    espacio_id: str
    usuario_id: str
    fecha: date
    hora_inicio: time
    hora_fin: time

class CancelarReserva(BaseModel):
    reserva_id: str
    usuario_id: str

class FechaBloqueada(BaseModel):
    fecha: date
    motivo: str

class EspacioCreate(BaseModel):
    nombre: str
    capacidad: int
    edificio_id: int

class HorarioUpdate(BaseModel):
    habilitado: bool
    hora_apertura: Optional[time] = None
    hora_cierre: Optional[time] = None


# ===== ADMIN LOGIN =====

class AdminLogin(BaseModel):
    password: str

@app.post("/admin/login")
async def admin_login(datos: AdminLogin):
    import secrets
    password_correcta = os.getenv("ADMIN_PASSWORD", "sL2#di!KBw")
    if datos.password != password_correcta:
        raise HTTPException(status_code=401, detail="Contraseña incorrecta")
    # Generar token simple
    token = secrets.token_hex(32)
    return {"token": token}

@app.post("/admin/verificar")
async def admin_verificar(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if not token or len(token) != 64:
        raise HTTPException(status_code=401, detail="Token inválido")
    return {"valido": True}

# ===== ADMIN DEL TENANT =====

class AdminTenantLogin(BaseModel):
    email: str
    password: str

class SetPassword(BaseModel):
    password: str

@app.post("/admin/tenant/login")
async def admin_tenant_login(request: Request, datos: AdminTenantLogin):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        usuario = await db.fetchrow(
            "SELECT * FROM usuarios WHERE email = $1 AND tenant_id = $2 AND rol = 'admin' AND activo = TRUE",
            datos.email, tenant["id"]
        )
        if not usuario:
            raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")
        if not usuario["password_hash"]:
            raise HTTPException(status_code=401, detail="Este usuario no tiene contraseña configurada")
        password_ok = bcrypt.checkpw(datos.password.encode(), usuario["password_hash"].encode())
        if not password_ok:
            raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")
        import secrets
        token = secrets.token_hex(32)
        return {
            "token": token,
            "usuario_id": str(usuario["id"]),
            "nombre": usuario["nombre"],
            "email": usuario["email"]
        }
    finally:
        await release_db(db)

@app.post("/usuarios/{usuario_id}/set-password")
async def set_admin_password(request: Request, usuario_id: str, datos: SetPassword):
    """Asigna contraseña a un usuario y lo promueve a admin."""
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        usuario = await db.fetchrow(
            "SELECT * FROM usuarios WHERE id = $1 AND tenant_id = $2",
            usuario_id, tenant["id"]
        )
        if not usuario:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        if len(datos.password) < 8:
            raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres")
        password_hash = bcrypt.hashpw(datos.password.encode(), bcrypt.gensalt()).decode()
        await db.execute(
            "UPDATE usuarios SET password_hash = $1, rol = 'admin' WHERE id = $2 AND tenant_id = $3",
            password_hash, usuario_id, tenant["id"]
        )
        return {"mensaje": f"{usuario['nombre']} ahora es administrador"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)


class AdminPerfilUpdate(BaseModel):
    nombre: Optional[str] = None
    email: Optional[str] = None
    password_actual: Optional[str] = None
    password_nueva: Optional[str] = None

@app.patch("/admin/perfil")
async def actualizar_perfil_admin(request: Request, datos: AdminPerfilUpdate):
    """Permite al admin del tenant actualizar su propio nombre, email y contraseña."""
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        # Identificar al admin por el header X-Admin-Email
        email_actual = request.headers.get("X-Admin-Email", "")
        if not email_actual:
            raise HTTPException(status_code=400, detail="Se requiere el header X-Admin-Email")
        usuario = await db.fetchrow(
            "SELECT * FROM usuarios WHERE email = $1 AND tenant_id = $2 AND rol = 'admin'",
            email_actual, tenant["id"]
        )
        if not usuario:
            raise HTTPException(status_code=404, detail="Administrador no encontrado")

        # Actualizar nombre
        if datos.nombre:
            await db.execute(
                "UPDATE usuarios SET nombre = $1 WHERE id = $2",
                datos.nombre, usuario["id"]
            )

        # Actualizar email
        if datos.email and datos.email != email_actual:
            existente = await db.fetchrow(
                "SELECT id FROM usuarios WHERE email = $1 AND tenant_id = $2",
                datos.email, tenant["id"]
            )
            if existente:
                raise HTTPException(status_code=400, detail="Ese email ya está en uso")
            await db.execute(
                "UPDATE usuarios SET email = $1 WHERE id = $2",
                datos.email, usuario["id"]
            )

        # Actualizar contraseña
        if datos.password_nueva:
            if not datos.password_actual:
                raise HTTPException(status_code=400, detail="Debés ingresar tu contraseña actual")
            if not bcrypt.checkpw(datos.password_actual.encode(), usuario["password_hash"].encode()):
                raise HTTPException(status_code=401, detail="La contraseña actual es incorrecta")
            if len(datos.password_nueva) < 8:
                raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 8 caracteres")
            nuevo_hash = bcrypt.hashpw(datos.password_nueva.encode(), bcrypt.gensalt()).decode()
            await db.execute(
                "UPDATE usuarios SET password_hash = $1 WHERE id = $2",
                nuevo_hash, usuario["id"]
            )

        return {"mensaje": "Perfil actualizado correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)


import json as json_module

class TenantPerfilUpdate(BaseModel):
    logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
    foto_url: Optional[str] = None
    color_primario: Optional[str] = None
    color_secundario: Optional[str] = None
    direccion: Optional[str] = None
    whatsapp: Optional[str] = None
    redes_sociales: Optional[list] = None

@app.patch("/tenant/perfil")
async def actualizar_perfil_tenant(request: Request, datos: TenantPerfilUpdate):
    """Permite al admin actualizar los datos de personalización del tenant."""
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        campos = []
        valores = []
        i = 1
        if datos.logo_url is not None:
            campos.append(f"logo_url = ${i}"); valores.append(datos.logo_url); i += 1
        if datos.favicon_url is not None:
            campos.append(f"favicon_url = ${i}"); valores.append(datos.favicon_url); i += 1
        if datos.foto_url is not None:
            campos.append(f"foto_url = ${i}"); valores.append(datos.foto_url); i += 1
        if datos.color_primario is not None:
            campos.append(f"color_primario = ${i}"); valores.append(datos.color_primario); i += 1
        if datos.color_secundario is not None:
            campos.append(f"color_secundario = ${i}"); valores.append(datos.color_secundario); i += 1
        if datos.direccion is not None:
            campos.append(f"direccion = ${i}"); valores.append(datos.direccion); i += 1
        if datos.whatsapp is not None:
            campos.append(f"whatsapp = ${i}"); valores.append(datos.whatsapp); i += 1
        if datos.redes_sociales is not None:
            campos.append(f"redes_sociales = ${i}"); valores.append(json_module.dumps(datos.redes_sociales)); i += 1
        if not campos:
            return {"mensaje": "No hay cambios para guardar"}
        valores.append(tenant["id"])
        await db.execute(
            f"UPDATE tenants SET {', '.join(campos)} WHERE id = ${i}",
            *valores
        )
        return {"mensaje": "Perfil del tenant actualizado correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)


@app.post("/tenant/imagen")
async def subir_imagen_tenant(request: Request):
    """Recibe una imagen en base64 y la guarda en Supabase Storage."""
    import base64
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        body = await request.json()
        imagen_b64 = body.get("imagen")
        tipo = body.get("tipo", "logo")  # logo, favicon, foto
        mime = body.get("mime", "image/png")
        
        if not imagen_b64:
            raise HTTPException(status_code=400, detail="Se requiere el campo imagen en base64")
        
        import httpx
        imagen_bytes = base64.b64decode(imagen_b64)
        extension = mime.split("/")[-1].replace("jpeg", "jpg")
        filename = f"{tenant['slug']}/{tipo}.{extension}"
        
        supabase_url = f"https://okkwfaouqdnbityotnje.supabase.co/storage/v1/object/logos/{filename}"
        supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
        
        if not supabase_key:
            raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_KEY no configurada")
        
        async with httpx.AsyncClient() as client:
            res = await client.put(
                supabase_url,
                content=imagen_bytes,
                headers={
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": mime,
                    "x-upsert": "true"
                }
            )
        
        if res.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Error al subir imagen: {res.text}")
        
        url_publica = f"https://okkwfaouqdnbityotnje.supabase.co/storage/v1/object/public/logos/{filename}"
        return {"url": url_publica}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

# ===== ENDPOINTS =====

@app.get("/")
async def inicio():
    return {"mensaje": "Sistema de Reserva de Espacios funcionando"}

@app.get("/tenant")
async def info_tenant(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        return {
            "nombre": tenant["nombre"],
            "slug": tenant["slug"],
            "logo_url": tenant["logo_url"],
            "favicon_url": tenant["favicon_url"],
            "color_primario": tenant["color_primario"],
            "color_secundario": tenant["color_secundario"],
            "plan": tenant["plan_id"],
            "direccion": tenant["direccion"],
            "whatsapp": tenant["whatsapp"],
            "redes_sociales": (json_module.loads(tenant["redes_sociales"]) if isinstance(tenant["redes_sociales"], str) else tenant["redes_sociales"]) or [],
            "foto_url": tenant["foto_url"],
        }
    finally:
        await release_db(db)

@app.get("/espacios")
async def listar_espacios(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        espacios = await db.fetch("""
            SELECT a.*, COALESCE(e.nombre, a.edificio, '') as nombre_edificio
            FROM aulas a
            LEFT JOIN edificios e ON a.edificio_id = e.id
            WHERE a.activa = TRUE AND a.tenant_id = $1
        """, tenant["id"])
        return [dict(e) for e in espacios]
    finally:
        await release_db(db)

@app.post("/espacios")
async def crear_espacio(request: Request, espacio: EspacioCreate):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        result = await db.fetchrow(
            """INSERT INTO aulas (nombre, capacidad, edificio_id, tenant_id)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            espacio.nombre, espacio.capacidad, espacio.edificio_id, tenant["id"]
        )
        return {"mensaje": "Espacio creado", "id": str(result["id"])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.patch("/espacios/{espacio_id}")
async def toggle_espacio(request: Request, espacio_id: str, datos: dict):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        await db.execute(
            "UPDATE aulas SET activa = $1 WHERE id = $2 AND tenant_id = $3",
            datos["activa"], espacio_id, tenant["id"]
        )
        return {"mensaje": "Espacio actualizado"}
    finally:
        await release_db(db)

@app.get("/disponibilidad/{espacio_id}/{fecha}")
async def consultar_disponibilidad(request: Request, espacio_id: str, fecha: date):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reservas = await db.fetch(
            """SELECT hora_inicio, hora_fin FROM reservas
               WHERE aula_id=$1 AND fecha=$2 AND estado='activa' AND tenant_id=$3""",
            espacio_id, fecha, tenant["id"]
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.post("/reservas")
async def crear_reserva(request: Request, reserva: ReservaCreate):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        tid = tenant["id"]

        ahora = datetime.now()
        fecha_hora_inicio = datetime.combine(reserva.fecha, reserva.hora_inicio)
        if fecha_hora_inicio < ahora:
            raise HTTPException(
                status_code=400,
                detail="La fecha y hora de reserva debe ser posterior a la actual"
            )

        fecha_bloqueada = await db.fetchrow(
            "SELECT motivo FROM fechas_bloqueadas WHERE fecha = $1 AND tenant_id = $2",
            reserva.fecha, tid
        )
        if fecha_bloqueada:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede reservar en esa fecha: {fecha_bloqueada['motivo']}"
            )

        dia_semana = reserva.fecha.weekday()
        config = await db.fetchrow(
            "SELECT * FROM configuracion_horarios WHERE dia_semana = $1 AND tenant_id = $2",
            dia_semana, tid
        )

        if not config or not config["habilitado"]:
            raise HTTPException(
                status_code=400,
                detail=f"No hay disponibilidad los {config['nombre_dia']}s"
            )

        if reserva.hora_inicio < config["hora_apertura"]:
            raise HTTPException(
                status_code=400,
                detail=f"El horario de apertura es a las {config['hora_apertura'].strftime('%H:%M')}"
            )

        if reserva.hora_fin > config["hora_cierre"]:
            raise HTTPException(
                status_code=400,
                detail=f"El horario de cierre es a las {config['hora_cierre'].strftime('%H:%M')}"
            )

        result = await db.fetchrow(
            """INSERT INTO reservas (aula_id, usuario_id, fecha, hora_inicio, hora_fin, tenant_id)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
            reserva.espacio_id, reserva.usuario_id, reserva.fecha,
            reserva.hora_inicio, reserva.hora_fin, tid
        )

        usuario = await db.fetchrow(
            "SELECT email, nombre FROM usuarios WHERE id = $1 AND tenant_id = $2",
            reserva.usuario_id, tid
        )
        espacio = await db.fetchrow(
            "SELECT nombre FROM aulas WHERE id = $1 AND tenant_id = $2",
            reserva.espacio_id, tid
        )
        if usuario:
            enviar_email(
                usuario["email"],
                "✅ Reserva confirmada",
                f"""
                <h2>¡Reserva confirmada!</h2>
                <p>Hola <b>{usuario['nombre']}</b>, tu reserva fue registrada correctamente.</p>
                <table style="border-collapse:collapse; margin-top:15px;">
                    <tr><td style="padding:8px; font-weight:bold">Espacio:</td><td style="padding:8px">{espacio['nombre']}</td></tr>
                    <tr><td style="padding:8px; font-weight:bold">Fecha:</td><td style="padding:8px">{reserva.fecha.strftime('%d/%m/%Y')}</td></tr>
                    <tr><td style="padding:8px; font-weight:bold">Horario:</td><td style="padding:8px">{reserva.hora_inicio.strftime('%H:%M')} - {reserva.hora_fin.strftime('%H:%M')}</td></tr>
                </table>
                <p style="margin-top:15px; color:#888">{tenant['nombre']}</p>
                """
            )
        return {"mensaje": "Reserva creada", "id": str(result["id"])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.delete("/reservas/{reserva_id}")
async def cancelar_reserva(request: Request, reserva_id: str, datos: CancelarReserva):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reserva = await db.fetchrow(
            "SELECT usuario_id FROM reservas WHERE id=$1 AND estado='activa' AND tenant_id=$2",
            reserva_id, tenant["id"]
        )
        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")
        if str(reserva["usuario_id"]) != datos.usuario_id:
            raise HTTPException(status_code=403, detail="Solo el usuario que creó la reserva puede cancelarla")
        await db.execute(
            "UPDATE reservas SET estado='cancelada' WHERE id=$1",
            reserva_id
        )
        usuario = await db.fetchrow(
            "SELECT email, nombre FROM usuarios WHERE id = $1",
            reserva["usuario_id"]
        )
        if usuario:
            enviar_email(
                usuario["email"],
                "❌ Reserva cancelada",
                f"""
                <h2>Reserva cancelada</h2>
                <p>Hola <b>{usuario['nombre']}</b>, tu reserva fue cancelada.</p>
                <p style="margin-top:15px; color:#888">Si no realizaste esta cancelación, contactá al administrador.</p>
                <p style="color:#888">{tenant['nombre']}</p>
                """
            )
        return {"mensaje": "Reserva cancelada correctamente"}
    finally:
        await release_db(db)

@app.delete("/reservas/{reserva_id}/admin")
async def cancelar_reserva_admin(request: Request, reserva_id: str):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reserva = await db.fetchrow(
            "SELECT id FROM reservas WHERE id=$1 AND estado='activa' AND tenant_id=$2",
            reserva_id, tenant["id"]
        )
        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")
        await db.execute(
            "UPDATE reservas SET estado='cancelada' WHERE id=$1",
            reserva_id
        )
        return {"mensaje": "Reserva cancelada correctamente"}
    finally:
        await release_db(db)

@app.get("/usuarios/buscar")
async def buscar_usuario(request: Request, email: str):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        usuario = await db.fetchrow(
            "SELECT * FROM usuarios WHERE email = $1 AND tenant_id = $2",
            email, tenant["id"]
        )
        if not usuario:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        return dict(usuario)
    finally:
        await release_db(db)

@app.post("/usuarios")
async def crear_usuario(request: Request, usuario: dict):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        result = await db.fetchrow(
            """INSERT INTO usuarios (email, nombre, rol, tenant_id)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            usuario["email"], usuario["nombre"], usuario.get("rol", "usuario"), tenant["id"]
        )
        return {"id": str(result["id"])}
    finally:
        await release_db(db)

@app.get("/usuarios")
async def listar_usuarios(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        usuarios = await db.fetch(
            "SELECT * FROM usuarios WHERE tenant_id = $1 ORDER BY nombre",
            tenant["id"]
        )
        return [dict(u) for u in usuarios]
    finally:
        await release_db(db)

@app.patch("/usuarios/{usuario_id}")
async def toggle_usuario(request: Request, usuario_id: str, datos: dict):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        await db.execute(
            "UPDATE usuarios SET activo = $1 WHERE id = $2 AND tenant_id = $3",
            datos["activo"], usuario_id, tenant["id"]
        )
        return {"mensaje": "Usuario actualizado"}
    finally:
        await release_db(db)

@app.get("/reservas/usuario")
async def reservas_por_usuario(request: Request, email: str):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reservas = await db.fetch(
            """SELECT r.id, r.fecha, r.hora_inicio, r.hora_fin, r.estado,
                      r.usuario_id, a.nombre as espacio_nombre
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               WHERE u.email = $1 AND r.tenant_id = $2
               ORDER BY r.fecha DESC, r.hora_inicio DESC""",
            email, tenant["id"]
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.get("/reservas")
async def listar_todas_reservas(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reservas = await db.fetch(
            """SELECT r.id, r.fecha, r.hora_inicio, r.hora_fin, r.estado,
                      r.usuario_id, a.nombre as espacio_nombre,
                      u.nombre as usuario_nombre, u.email as usuario_email
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               WHERE r.tenant_id = $1
               ORDER BY r.fecha DESC, r.hora_inicio DESC""",
            tenant["id"]
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.get("/reservas/calendario")
async def reservas_calendario(request: Request, fecha_inicio: date, fecha_fin: date):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reservas = await db.fetch(
            """SELECT r.id, r.fecha, r.hora_inicio, r.hora_fin,
                      a.id as espacio_id, a.nombre as espacio_nombre,
                      u.nombre as usuario_nombre
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               WHERE r.fecha BETWEEN $1 AND $2
               AND r.estado = 'activa'
               AND r.tenant_id = $3
               ORDER BY r.fecha, r.hora_inicio""",
            fecha_inicio, fecha_fin, tenant["id"]
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.get("/fechas-bloqueadas")
async def listar_fechas_bloqueadas(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        fechas = await db.fetch(
            "SELECT * FROM fechas_bloqueadas WHERE tenant_id = $1 ORDER BY fecha",
            tenant["id"]
        )
        return [dict(f) for f in fechas]
    finally:
        await release_db(db)

@app.post("/fechas-bloqueadas")
async def agregar_fecha_bloqueada(request: Request, datos: FechaBloqueada):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        await db.execute(
            "INSERT INTO fechas_bloqueadas (fecha, motivo, tenant_id) VALUES ($1, $2, $3)",
            datos.fecha, datos.motivo, tenant["id"]
        )
        return {"mensaje": f"Fecha {datos.fecha} bloqueada correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.delete("/fechas-bloqueadas/{fecha}")
async def eliminar_fecha_bloqueada(request: Request, fecha: date):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        result = await db.execute(
            "DELETE FROM fechas_bloqueadas WHERE fecha = $1 AND tenant_id = $2",
            fecha, tenant["id"]
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Fecha no encontrada")
        return {"mensaje": "Fecha desbloqueada correctamente"}
    finally:
        await release_db(db)

@app.get("/horarios")
async def listar_horarios(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        horarios = await db.fetch(
            "SELECT * FROM configuracion_horarios WHERE tenant_id = $1 ORDER BY dia_semana",
            tenant["id"]
        )
        return [dict(h) for h in horarios]
    finally:
        await release_db(db)

@app.patch("/horarios/{dia_semana}")
async def actualizar_horario(request: Request, dia_semana: int, datos: HorarioUpdate):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        await db.execute(
            """UPDATE configuracion_horarios
               SET habilitado = $1, hora_apertura = $2, hora_cierre = $3
               WHERE dia_semana = $4 AND tenant_id = $5""",
            datos.habilitado, datos.hora_apertura, datos.hora_cierre, dia_semana, tenant["id"]
        )
        return {"mensaje": "Horario actualizado"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.get("/edificios")
async def listar_edificios(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        edificios = await db.fetch(
            "SELECT * FROM edificios WHERE activo = TRUE AND tenant_id = $1",
            tenant["id"]
        )
        return [dict(e) for e in edificios]
    finally:
        await release_db(db)

@app.post("/edificios")
async def crear_edificio(request: Request, datos: dict):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        result = await db.fetchrow(
            "INSERT INTO edificios (nombre, direccion, tenant_id) VALUES ($1, $2, $3) RETURNING id",
            datos["nombre"], datos.get("direccion", ""), tenant["id"]
        )
        return {"mensaje": "Edificio creado", "id": result["id"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.patch("/edificios/{edificio_id}")
async def actualizar_edificio(request: Request, edificio_id: int, datos: dict):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        if "nombre" in datos:
            await db.execute(
                "UPDATE edificios SET nombre = $1 WHERE id = $2 AND tenant_id = $3",
                datos["nombre"], edificio_id, tenant["id"]
            )
        if "activo" in datos:
            await db.execute(
                "UPDATE edificios SET activo = $1 WHERE id = $2 AND tenant_id = $3",
                datos["activo"], edificio_id, tenant["id"]
            )
        return {"mensaje": "Edificio actualizado"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.get("/edificios/{edificio_id}/espacios")
async def espacios_por_edificio(request: Request, edificio_id: int):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        espacios = await db.fetch(
            "SELECT * FROM aulas WHERE edificio_id = $1 AND activa = TRUE AND tenant_id = $2",
            edificio_id, tenant["id"]
        )
        return [dict(e) for e in espacios]
    finally:
        await release_db(db)

@app.get("/reservas/exportar")
async def exportar_reservas(request: Request):
    db = await get_db()
    try:
        tenant = await get_tenant(request, db)
        reservas = await db.fetch(
            """SELECT r.fecha, r.hora_inicio, r.hora_fin, r.estado,
                      a.nombre as espacio_nombre,
                      u.nombre as usuario_nombre, u.email as usuario_email
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               WHERE r.tenant_id = $1
               ORDER BY r.fecha DESC, r.hora_inicio DESC""",
            tenant["id"]
        )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Reservas"

        encabezados = ["Fecha", "Hora inicio", "Hora fin", "Espacio", "Usuario", "Email", "Estado"]
        for col, enc in enumerate(encabezados, 1):
            celda = ws.cell(row=1, column=col, value=enc)
            celda.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
            celda.fill = openpyxl.styles.PatternFill("solid", fgColor="1B4F8A")

        for fila, r in enumerate(reservas, 2):
            ws.cell(row=fila, column=1, value=r["fecha"].strftime("%d/%m/%Y"))
            ws.cell(row=fila, column=2, value=r["hora_inicio"].strftime("%H:%M"))
            ws.cell(row=fila, column=3, value=r["hora_fin"].strftime("%H:%M"))
            ws.cell(row=fila, column=4, value=r["espacio_nombre"])
            ws.cell(row=fila, column=5, value=r["usuario_nombre"])
            ws.cell(row=fila, column=6, value=r["usuario_email"])
            ws.cell(row=fila, column=7, value=r["estado"])

        anchos = [12, 12, 12, 15, 25, 30, 12]
        for col, ancho in enumerate(anchos, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = ancho

        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=reservas.xlsx"}
        )
    finally:
        await release_db(db)


# ===== REGISTRO DE NUEVO TENANT =====

class RegistroCreate(BaseModel):
    nombre: str
    tipo: str
    email_admin: str
    nombre_admin: str
    plan_id: str
    slug: str

@app.post("/registro")
async def registrar_tenant(datos: RegistroCreate):
    db = await get_db()
    try:
        # Validar que el slug no esté tomado
        existente = await db.fetchrow(
            "SELECT id FROM tenants WHERE slug = $1",
            datos.slug
        )
        if existente:
            raise HTTPException(
                status_code=400,
                detail="Esa URL ya está en uso. Por favor elegí otra."
            )

        # Validar que el email no esté registrado
        email_existente = await db.fetchrow(
            "SELECT id FROM tenants WHERE email_admin = $1",
            datos.email_admin
        )
        if email_existente:
            raise HTTPException(
                status_code=400,
                detail="Ya existe una cuenta registrada con ese email."
            )

        # Crear el tenant
        tenant = await db.fetchrow(
            """INSERT INTO tenants (nombre, slug, email_admin, plan_id, trial_hasta, suscripcion_activa)
               VALUES ($1, $2, $3, $4, CURRENT_DATE + INTERVAL '30 days', FALSE)
               RETURNING id, nombre, slug, trial_hasta""",
            datos.nombre, datos.slug, datos.email_admin, datos.plan_id
        )

        tenant_id = tenant["id"]

        # Crear horarios por defecto (lunes a viernes 8:00-20:00, finde cerrado)
        dias = [
            (0, "Lunes",     True,  time(8, 0), time(20, 0)),
            (1, "Martes",    True,  time(8, 0), time(20, 0)),
            (2, "Miércoles", True,  time(8, 0), time(20, 0)),
            (3, "Jueves",    True,  time(8, 0), time(20, 0)),
            (4, "Viernes",   True,  time(8, 0), time(20, 0)),
            (5, "Sábado",    False, None,       None),
            (6, "Domingo",   False, None,       None),
        ]

        for dia_semana, nombre_dia, habilitado, apertura, cierre in dias:
            await db.execute(
                """INSERT INTO configuracion_horarios
                   (dia_semana, nombre_dia, habilitado, hora_apertura, hora_cierre, tenant_id)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                dia_semana, nombre_dia, habilitado, apertura, cierre, tenant_id
            )

        # Enviar email de bienvenida
        trial_hasta = tenant["trial_hasta"].strftime("%d/%m/%Y")
        enviar_email(
            datos.email_admin,
            "🎉 ¡Bienvenido a reservatuespacio.com!",
            f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: #2C3E50; padding: 32px; text-align: center; border-radius: 12px 12px 0 0;">
                    <h1 style="color: #71D997; margin: 0; font-size: 28px;">¡Bienvenido a ReservaSpace!</h1>
                </div>
                <div style="background: #F9F9FB; padding: 32px; border-radius: 0 0 12px 12px;">
                    <p style="font-size: 16px; color: #2C3E50;">Hola <b>{datos.nombre_admin}</b>,</p>
                    <p style="color: #4A5568; line-height: 1.7;">
                        Tu cuenta para <b>{datos.nombre}</b> fue creada exitosamente.
                        Tenés <b>30 días de prueba gratuita</b> hasta el <b>{trial_hasta}</b>.
                    </p>
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px; margin: 24px 0;">
                        <p style="margin: 0 0 8px; color: #4A5568;"><b>Tu URL de acceso:</b></p>
                        <p style="margin: 0; font-size: 18px; color: #2C3E50; font-weight: bold;">
                            {datos.slug}.reservatuespacio.com
                        </p>
                    </div>
                    <p style="color: #4A5568; line-height: 1.7;">
                        Para empezar, entrá a tu panel de administración y configurá tus espacios y horarios.
                    </p>
                    <div style="text-align: center; margin-top: 28px;">
                        <a href="https://claudiaacreativity.github.io/reserva-aulas/admin.html"
                           style="background: #71D997; color: #2C3E50; padding: 14px 32px;
                                  border-radius: 50px; text-decoration: none; font-weight: bold; font-size: 15px;">
                            Ir al panel de administración →
                        </a>
                    </div>
                    <p style="margin-top: 28px; color: #888; font-size: 13px; text-align: center;">
                        ¿Tenés dudas? Escribinos a soporte@reservatuespacio.com
                    </p>
                </div>
            </div>
            """
        )

        return {
            "mensaje": "Cuenta creada exitosamente",
            "tenant_id": str(tenant_id),
            "slug": datos.slug,
            "trial_hasta": trial_hasta
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)


# ===== SUPERADMIN =====

def verificar_superadmin(request: Request):
    token = request.headers.get("X-Superadmin-Token", "")
    password_correcta = os.getenv("ADMIN_PASSWORD", "sL2#di!KBw")
    if token != password_correcta:
        raise HTTPException(status_code=401, detail="No autorizado")

class TenantUpdate(BaseModel):
    activo: Optional[bool] = None
    suscripcion_activa: Optional[bool] = None
    plan_id: Optional[str] = None
    trial_hasta: Optional[date] = None

class PlanUpdate(BaseModel):
    nombre: Optional[str] = None
    precio: Optional[float] = None
    descripcion: Optional[str] = None

class TenantCreate(BaseModel):
    nombre: str
    slug: str
    email_admin: str
    nombre_admin: str
    plan_id: str
    trial_dias: int = 30

@app.get("/superadmin/tenants")
async def superadmin_listar_tenants(request: Request):
    verificar_superadmin(request)
    db = await get_db()
    try:
        tenants = await db.fetch("""
            SELECT
                t.*,
                p.nombre as plan_nombre,
                p.precio_mensual as plan_precio,
                (SELECT COUNT(*) FROM usuarios u WHERE u.tenant_id = t.id) as total_usuarios,
                (SELECT COUNT(*) FROM reservas r WHERE r.tenant_id = t.id AND r.estado = 'activa') as total_reservas
            FROM tenants t
            LEFT JOIN planes p ON t.plan_id = p.id
            ORDER BY t.activo DESC, t.trial_hasta DESC
        """)
        return [dict(t) for t in tenants]
    finally:
        await release_db(db)

@app.patch("/superadmin/tenants/{tenant_id}")
async def superadmin_actualizar_tenant(request: Request, tenant_id: str, datos: TenantUpdate):
    verificar_superadmin(request)
    db = await get_db()
    try:
        tenant = await db.fetchrow("SELECT id FROM tenants WHERE id = $1", tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        if datos.activo is not None:
            await db.execute("UPDATE tenants SET activo = $1 WHERE id = $2", datos.activo, tenant_id)
        if datos.suscripcion_activa is not None:
            await db.execute("UPDATE tenants SET suscripcion_activa = $1 WHERE id = $2", datos.suscripcion_activa, tenant_id)
        if datos.plan_id is not None:
            await db.execute("UPDATE tenants SET plan_id = $1 WHERE id = $2", datos.plan_id, tenant_id)
        if datos.trial_hasta is not None:
            await db.execute("UPDATE tenants SET trial_hasta = $1 WHERE id = $2", datos.trial_hasta, tenant_id)
        return {"mensaje": "Tenant actualizado correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.post("/superadmin/tenants")
async def superadmin_crear_tenant(request: Request, datos: TenantCreate):
    verificar_superadmin(request)
    db = await get_db()
    try:
        existente = await db.fetchrow("SELECT id FROM tenants WHERE slug = $1", datos.slug)
        if existente:
            raise HTTPException(status_code=400, detail="Ese slug ya está en uso")
        email_existente = await db.fetchrow("SELECT id FROM tenants WHERE email_admin = $1", datos.email_admin)
        if email_existente:
            raise HTTPException(status_code=400, detail="Ya existe una cuenta con ese email")
        tenant = await db.fetchrow(
            """INSERT INTO tenants (nombre, slug, email_admin, plan_id, trial_hasta, suscripcion_activa)
               VALUES ($1, $2, $3, $4, CURRENT_DATE + ($5 || ' days')::interval, FALSE)
               RETURNING id, nombre, slug, trial_hasta""",
            datos.nombre, datos.slug, datos.email_admin, datos.plan_id, str(datos.trial_dias)
        )
        tenant_id = tenant["id"]
        dias = [
            (0, "Lunes",     True,  time(8, 0), time(20, 0)),
            (1, "Martes",    True,  time(8, 0), time(20, 0)),
            (2, "Miércoles", True,  time(8, 0), time(20, 0)),
            (3, "Jueves",    True,  time(8, 0), time(20, 0)),
            (4, "Viernes",   True,  time(8, 0), time(20, 0)),
            (5, "Sábado",    False, None,        None),
            (6, "Domingo",   False, None,        None),
        ]
        for dia_semana, nombre_dia, habilitado, apertura, cierre in dias:
            await db.execute(
                """INSERT INTO configuracion_horarios
                   (dia_semana, nombre_dia, habilitado, hora_apertura, hora_cierre, tenant_id)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                dia_semana, nombre_dia, habilitado, apertura, cierre, tenant_id
            )
        trial_hasta = tenant["trial_hasta"].strftime("%d/%m/%Y")
        enviar_email(
            datos.email_admin,
            "Bienvenido a reservatuespacio.com!",
            f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: #2C3E50; padding: 32px; text-align: center; border-radius: 12px 12px 0 0;">
                    <h1 style="color: #71D997; margin: 0; font-size: 28px;">Bienvenido a reservatuespacio.com!</h1>
                </div>
                <div style="background: #F9F9FB; padding: 32px; border-radius: 0 0 12px 12px;">
                    <p style="font-size: 16px; color: #2C3E50;">Hola <b>{datos.nombre_admin}</b>,</p>
                    <p style="color: #4A5568; line-height: 1.7;">
                        Tu cuenta para <b>{datos.nombre}</b> fue creada exitosamente.
                        Tenes <b>{datos.trial_dias} dias de prueba gratuita</b> hasta el <b>{trial_hasta}</b>.
                    </p>
                    <div style="background: white; border: 1px solid #e0e0e0; border-radius: 10px; padding: 20px; margin: 24px 0;">
                        <p style="margin: 0 0 8px; color: #4A5568;"><b>Tu URL de acceso:</b></p>
                        <p style="margin: 0; font-size: 18px; color: #2C3E50; font-weight: bold;">
                            {datos.slug}.reservatuespacio.com
                        </p>
                    </div>
                    <div style="text-align: center; margin-top: 28px;">
                        <a href="https://claudiaacreativity.github.io/reserva-aulas/admin.html"
                           style="background: #71D997; color: #2C3E50; padding: 14px 32px;
                                  border-radius: 50px; text-decoration: none; font-weight: bold; font-size: 15px;">
                            Ir al panel de administracion
                        </a>
                    </div>
                </div>
            </div>
            """
        )
        return {
            "mensaje": "Tenant creado correctamente",
            "tenant_id": str(tenant_id),
            "slug": datos.slug,
            "trial_hasta": trial_hasta
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.get("/superadmin/planes")
async def superadmin_listar_planes(request: Request):
    verificar_superadmin(request)
    db = await get_db()
    try:
        planes = await db.fetch("SELECT * FROM planes ORDER BY precio_mensual")
        return [dict(p) for p in planes]
    finally:
        await release_db(db)

@app.patch("/superadmin/planes/{plan_id}")
async def superadmin_actualizar_plan(request: Request, plan_id: str, datos: PlanUpdate):
    verificar_superadmin(request)
    db = await get_db()
    try:
        plan = await db.fetchrow("SELECT id FROM planes WHERE id = $1", plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Plan no encontrado")
        if datos.nombre is not None:
            await db.execute("UPDATE planes SET nombre = $1 WHERE id = $2", datos.nombre, plan_id)
        if datos.precio_mensual is not None:
            await db.execute("UPDATE planes SET precio_mensual = $1 WHERE id = $2", datos.precio_mensual, plan_id)
        if datos.descripcion is not None:
            await db.execute("UPDATE planes SET descripcion = $1 WHERE id = $2", datos.descripcion, plan_id)
        return {"mensaje": "Plan actualizado correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.get("/superadmin/stats")
async def superadmin_stats(request: Request):
    verificar_superadmin(request)
    db = await get_db()
    try:
        stats = await db.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM tenants) as total_tenants,
                (SELECT COUNT(*) FROM tenants WHERE activo = TRUE) as tenants_activos,
                (SELECT COUNT(*) FROM tenants WHERE suscripcion_activa = TRUE) as tenants_pagos,
                (SELECT COUNT(*) FROM tenants WHERE activo = TRUE AND suscripcion_activa = FALSE AND trial_hasta >= CURRENT_DATE) as tenants_trial,
                (SELECT COUNT(*) FROM tenants WHERE activo = TRUE AND suscripcion_activa = FALSE AND trial_hasta < CURRENT_DATE) as tenants_vencidos,
                (SELECT COUNT(*) FROM usuarios) as total_usuarios,
                (SELECT COUNT(*) FROM reservas WHERE estado = 'activa') as total_reservas
        """)
        return dict(stats)
    finally:
        await release_db(db)

# ===== SUPERADMIN — FACTURACIÓN =====

class PagoCreate(BaseModel):
    tenant_id: str
    monto: float
    moneda: str = "USD"
    metodo: str
    referencia: Optional[str] = None
    notas: Optional[str] = None
    fecha: date
    registrado_por: Optional[str] = None

@app.get("/superadmin/facturacion")
async def superadmin_facturacion(request: Request):
    verificar_superadmin(request)
    db = await get_db()
    try:
        ingreso = await db.fetchrow("""
            SELECT COALESCE(SUM(p.precio_mensual), 0) as ingreso_mensual
            FROM tenants t
            JOIN planes p ON t.plan_id = p.id
            WHERE t.suscripcion_activa = TRUE AND t.activo = TRUE
        """)
        alertas = await db.fetch("""
            SELECT t.nombre, t.email_admin, t.slug, t.trial_hasta,
                   (t.trial_hasta - CURRENT_DATE) as dias_restantes
            FROM tenants t
            WHERE t.activo = TRUE
              AND t.suscripcion_activa = FALSE
              AND t.trial_hasta BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
            ORDER BY t.trial_hasta
        """)
        pagando = await db.fetch("""
            SELECT t.nombre, t.email_admin, t.slug, p.nombre as plan_nombre, p.precio_mensual as plan_precio
            FROM tenants t
            JOIN planes p ON t.plan_id = p.id
            WHERE t.suscripcion_activa = TRUE AND t.activo = TRUE
            ORDER BY t.nombre
        """)
        historial = await db.fetch("""
            SELECT pg.*, t.nombre as tenant_nombre, t.slug as tenant_slug
            FROM pagos pg
            JOIN tenants t ON pg.tenant_id = t.id
            ORDER BY pg.fecha DESC, pg.created_at DESC
            LIMIT 100
        """)
        return {
            "ingreso_mensual": float(ingreso["ingreso_mensual"]),
            "alertas_trial": [dict(a) for a in alertas],
            "tenants_pagando": [dict(p) for p in pagando],
            "historial_pagos": [dict(h) for h in historial]
        }
    finally:
        await release_db(db)

@app.post("/superadmin/pagos")
async def superadmin_registrar_pago(request: Request, datos: PagoCreate):
    verificar_superadmin(request)
    db = await get_db()
    try:
        tenant = await db.fetchrow("SELECT id, nombre FROM tenants WHERE id = $1", datos.tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        pago = await db.fetchrow(
            """INSERT INTO pagos (tenant_id, monto, moneda, metodo, referencia, notas, fecha, registrado_por)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id""",
            datos.tenant_id, datos.monto, datos.moneda, datos.metodo,
            datos.referencia, datos.notas, datos.fecha, datos.registrado_por
        )
        return {"mensaje": f"Pago registrado para {tenant['nombre']}", "id": str(pago["id"])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.delete("/superadmin/pagos/{pago_id}")
async def superadmin_eliminar_pago(request: Request, pago_id: str):
    verificar_superadmin(request)
    db = await get_db()
    try:
        result = await db.execute("DELETE FROM pagos WHERE id = $1", pago_id)
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Pago no encontrado")
        return {"mensaje": "Pago eliminado"}
    finally:
        await release_db(db)

@app.get("/superadmin/facturacion/exportar")
async def superadmin_exportar_facturacion(request: Request, token: Optional[str] = None):
    if token:
        password_correcta = os.getenv("ADMIN_PASSWORD", "sL2#di!KBw")
        if token != password_correcta:
            raise HTTPException(status_code=401, detail="No autorizado")
    else:
        verificar_superadmin(request)
    db = await get_db()
    try:
        pagos = await db.fetch("""
            SELECT pg.fecha, t.nombre as tenant, t.slug, pg.monto, pg.moneda,
                   pg.metodo, pg.referencia, pg.notas, pg.registrado_por, pg.created_at
            FROM pagos pg
            JOIN tenants t ON pg.tenant_id = t.id
            ORDER BY pg.fecha DESC
        """)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Facturación"
        encabezados = ["Fecha", "Tenant", "Slug", "Monto", "Moneda", "Método", "Referencia", "Notas", "Registrado por", "Fecha registro"]
        for col, enc in enumerate(encabezados, 1):
            celda = ws.cell(row=1, column=col, value=enc)
            celda.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
            celda.fill = openpyxl.styles.PatternFill("solid", fgColor="2C3E50")
        for fila, p in enumerate(pagos, 2):
            ws.cell(row=fila, column=1, value=p["fecha"].strftime("%d/%m/%Y"))
            ws.cell(row=fila, column=2, value=p["tenant"])
            ws.cell(row=fila, column=3, value=p["slug"])
            ws.cell(row=fila, column=4, value=float(p["monto"]))
            ws.cell(row=fila, column=5, value=p["moneda"])
            ws.cell(row=fila, column=6, value=p["metodo"])
            ws.cell(row=fila, column=7, value=p["referencia"] or "")
            ws.cell(row=fila, column=8, value=p["notas"] or "")
            ws.cell(row=fila, column=9, value=p["registrado_por"] or "")
            ws.cell(row=fila, column=10, value=p["created_at"].strftime("%d/%m/%Y %H:%M"))
        anchos = [12, 25, 15, 10, 8, 15, 20, 25, 20, 18]
        for col, ancho in enumerate(anchos, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = ancho
        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=facturacion.xlsx"}
        )
    finally:
        await release_db(db)
