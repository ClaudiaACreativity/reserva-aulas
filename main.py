from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import date, time, datetime
from typing import Optional
import asyncpg
import os
import resend
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Reserva de Aulas")

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

# ===== EMAIL =====
def enviar_email(destinatario: str, asunto: str, cuerpo: str):
    try:
        resend.Emails.send({
            "from": "Reserva de Aulas <onboarding@resend.dev>",
            "to": destinatario,
            "subject": asunto,
            "html": cuerpo
        })
    except Exception as e:
        print(f"Error al enviar email: {e}")

# Modelos
class ReservaCreate(BaseModel):
    aula_id: str
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

class AulaCreate(BaseModel):
    nombre: str
    capacidad: int
    edificio_id: int

class HorarioUpdate(BaseModel):
    habilitado: bool
    hora_apertura: Optional[time] = None
    hora_cierre: Optional[time] = None

# ===== ENDPOINTS =====

@app.get("/")
async def inicio():
    return {"mensaje": "Sistema de Reserva de Aulas funcionando"}

@app.get("/aulas")
async def listar_aulas():
    db = await get_db()
    try:
        aulas = await db.fetch("SELECT * FROM aulas WHERE activa = TRUE")
        return [dict(a) for a in aulas]
    finally:
        await release_db(db)

@app.post("/aulas")
async def crear_aula(aula: AulaCreate):
    db = await get_db()
    try:
        result = await db.fetchrow(
            """INSERT INTO aulas (nombre, capacidad, edificio_id)
               VALUES ($1, $2, $3) RETURNING id""",
            aula.nombre, aula.capacidad, aula.edificio_id
        )
        return {"mensaje": "Aula creada", "id": str(result["id"])}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.patch("/aulas/{aula_id}")
async def toggle_aula(aula_id: str, datos: dict):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE aulas SET activa = $1 WHERE id = $2",
            datos["activa"], aula_id
        )
        return {"mensaje": "Aula actualizada"}
    finally:
        await release_db(db)

@app.get("/disponibilidad/{aula_id}/{fecha}")
async def consultar_disponibilidad(aula_id: str, fecha: date):
    db = await get_db()
    try:
        reservas = await db.fetch(
            "SELECT hora_inicio, hora_fin FROM reservas WHERE aula_id=$1 AND fecha=$2 AND estado='activa'",
            aula_id, fecha
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.post("/reservas")
async def crear_reserva(reserva: ReservaCreate):
    db = await get_db()
    try:
        # Verificar que la fecha no sea pasada
        ahora = datetime.now()
        fecha_hora_inicio = datetime.combine(reserva.fecha, reserva.hora_inicio)
        if fecha_hora_inicio < ahora:
            raise HTTPException(
                status_code=400,
                detail="La fecha y hora de reserva debe ser posterior a la actual"
            )

        # Verificar si la fecha está bloqueada
        fecha_bloqueada = await db.fetchrow(
            "SELECT motivo FROM fechas_bloqueadas WHERE fecha = $1",
            reserva.fecha
        )
        if fecha_bloqueada:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede reservar en esa fecha: {fecha_bloqueada['motivo']}"
            )

        # Obtener el día de la semana (0=Lunes, 6=Domingo)
        dia_semana = reserva.fecha.weekday()

        # Consultar configuración de ese día
        config = await db.fetchrow(
            "SELECT * FROM configuracion_horarios WHERE dia_semana = $1",
            dia_semana
        )

        # Verificar si el día está habilitado
        if not config or not config["habilitado"]:
            raise HTTPException(
                status_code=400,
                detail=f"El instituto no abre los {config['nombre_dia']}s"
            )

        # Verificar hora de apertura
        if reserva.hora_inicio < config["hora_apertura"]:
            raise HTTPException(
                status_code=400,
                detail=f"El instituto abre a las {config['hora_apertura'].strftime('%H:%M')} ese día"
            )

        # Verificar hora de cierre
        if reserva.hora_fin > config["hora_cierre"]:
            raise HTTPException(
                status_code=400,
                detail=f"El instituto cierra a las {config['hora_cierre'].strftime('%H:%M')} ese día"
            )

        # Crear la reserva
        result = await db.fetchrow(
            """INSERT INTO reservas (aula_id, usuario_id, fecha, hora_inicio, hora_fin)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            reserva.aula_id, reserva.usuario_id, reserva.fecha,
            reserva.hora_inicio, reserva.hora_fin
        )

        # Obtener datos para el email
        usuario = await db.fetchrow(
            "SELECT email, nombre FROM usuarios WHERE id = $1",
            reserva.usuario_id
        )
        aula = await db.fetchrow(
            "SELECT nombre FROM aulas WHERE id = $1",
            reserva.aula_id
        )
        if usuario:
            enviar_email(
                usuario["email"],
                "✅ Reserva confirmada",
                f"""
                <h2>¡Reserva confirmada!</h2>
                <p>Hola <b>{usuario['nombre']}</b>, tu reserva fue registrada correctamente.</p>
                <table style="border-collapse:collapse; margin-top:15px;">
                    <tr><td style="padding:8px; font-weight:bold">Aula:</td><td style="padding:8px">{aula['nombre']}</td></tr>
                    <tr><td style="padding:8px; font-weight:bold">Fecha:</td><td style="padding:8px">{reserva.fecha.strftime('%d/%m/%Y')}</td></tr>
                    <tr><td style="padding:8px; font-weight:bold">Horario:</td><td style="padding:8px">{reserva.hora_inicio.strftime('%H:%M')} - {reserva.hora_fin.strftime('%H:%M')}</td></tr>
                </table>
                <p style="margin-top:15px; color:#888">Sistema de Reserva de Aulas</p>
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
async def cancelar_reserva(reserva_id: str, datos: CancelarReserva):
    db = await get_db()
    try:
        reserva = await db.fetchrow(
            "SELECT usuario_id FROM reservas WHERE id=$1 AND estado='activa'",
            reserva_id
        )
        if not reserva:
            raise HTTPException(status_code=404, detail="Reserva no encontrada")
        if str(reserva["usuario_id"]) != datos.usuario_id:
            raise HTTPException(status_code=403, detail="Solo el docente que creó la reserva puede cancelarla")
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
                <p style="color:#888">Sistema de Reserva de Aulas</p>
                """
            )
        return {"mensaje": "Reserva cancelada correctamente"}
    finally:
        await release_db(db)

@app.delete("/reservas/{reserva_id}/admin")
async def cancelar_reserva_admin(reserva_id: str):
    db = await get_db()
    try:
        reserva = await db.fetchrow(
            "SELECT id FROM reservas WHERE id=$1 AND estado='activa'",
            reserva_id
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
async def buscar_usuario(email: str):
    db = await get_db()
    try:
        usuario = await db.fetchrow(
            "SELECT * FROM usuarios WHERE email = $1",
            email
        )
        if not usuario:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        return dict(usuario)
    finally:
        await release_db(db)

@app.post("/usuarios")
async def crear_usuario(usuario: dict):
    db = await get_db()
    try:
        result = await db.fetchrow(
            """INSERT INTO usuarios (email, nombre, rol)
               VALUES ($1, $2, $3) RETURNING id""",
            usuario["email"], usuario["nombre"], usuario.get("rol", "docente")
        )
        return {"id": str(result["id"])}
    finally:
        await release_db(db)

@app.get("/usuarios")
async def listar_usuarios():
    db = await get_db()
    try:
        usuarios = await db.fetch("SELECT * FROM usuarios ORDER BY nombre")
        return [dict(u) for u in usuarios]
    finally:
        await release_db(db)

@app.patch("/usuarios/{usuario_id}")
async def toggle_usuario(usuario_id: str, datos: dict):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE usuarios SET activo = $1 WHERE id = $2",
            datos["activo"], usuario_id
        )
        return {"mensaje": "Usuario actualizado"}
    finally:
        await release_db(db)

@app.get("/reservas/usuario")
async def reservas_por_usuario(email: str):
    db = await get_db()
    try:
        reservas = await db.fetch(
            """SELECT r.id, r.fecha, r.hora_inicio, r.hora_fin, r.estado,
                      r.usuario_id, a.nombre as aula_nombre
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               WHERE u.email = $1
               ORDER BY r.fecha DESC, r.hora_inicio DESC""",
            email
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.get("/reservas")
async def listar_todas_reservas():
    db = await get_db()
    try:
        reservas = await db.fetch(
            """SELECT r.id, r.fecha, r.hora_inicio, r.hora_fin, r.estado,
                      r.usuario_id, a.nombre as aula_nombre,
                      u.nombre as docente_nombre, u.email as docente_email
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               ORDER BY r.fecha DESC, r.hora_inicio DESC"""
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.get("/reservas/calendario")
async def reservas_calendario(fecha_inicio: date, fecha_fin: date):
    db = await get_db()
    try:
        reservas = await db.fetch(
            """SELECT r.id, r.fecha, r.hora_inicio, r.hora_fin,
                      a.id as aula_id, a.nombre as aula_nombre,
                      u.nombre as docente_nombre
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               WHERE r.fecha BETWEEN $1 AND $2
               AND r.estado = 'activa'
               ORDER BY r.fecha, r.hora_inicio""",
            fecha_inicio, fecha_fin
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.get("/fechas-bloqueadas")
async def listar_fechas_bloqueadas():
    db = await get_db()
    try:
        fechas = await db.fetch(
            "SELECT * FROM fechas_bloqueadas ORDER BY fecha"
        )
        return [dict(f) for f in fechas]
    finally:
        await release_db(db)

@app.post("/fechas-bloqueadas")
async def agregar_fecha_bloqueada(datos: FechaBloqueada):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO fechas_bloqueadas (fecha, motivo) VALUES ($1, $2)",
            datos.fecha, datos.motivo
        )
        return {"mensaje": f"Fecha {datos.fecha} bloqueada correctamente"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.delete("/fechas-bloqueadas/{fecha}")
async def eliminar_fecha_bloqueada(fecha: date):
    db = await get_db()
    try:
        result = await db.execute(
            "DELETE FROM fechas_bloqueadas WHERE fecha = $1", fecha
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Fecha no encontrada")
        return {"mensaje": "Fecha desbloqueada correctamente"}
    finally:
        await release_db(db)

@app.get("/horarios")
async def listar_horarios():
    db = await get_db()
    try:
        horarios = await db.fetch(
            "SELECT * FROM configuracion_horarios ORDER BY dia_semana"
        )
        return [dict(h) for h in horarios]
    finally:
        await release_db(db)

@app.patch("/horarios/{dia_semana}")
async def actualizar_horario(dia_semana: int, datos: HorarioUpdate):
    db = await get_db()
    try:
        await db.execute(
            """UPDATE configuracion_horarios
               SET habilitado = $1, hora_apertura = $2, hora_cierre = $3
               WHERE dia_semana = $4""",
            datos.habilitado, datos.hora_apertura, datos.hora_cierre, dia_semana
        )
        return {"mensaje": "Horario actualizado"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.get("/edificios")
async def listar_edificios():
    db = await get_db()
    try:
        edificios = await db.fetch("SELECT * FROM edificios WHERE activo = TRUE")
        return [dict(e) for e in edificios]
    finally:
        await release_db(db)

@app.post("/edificios")
async def crear_edificio(datos: dict):
    db = await get_db()
    try:
        result = await db.fetchrow(
            "INSERT INTO edificios (nombre, direccion) VALUES ($1, $2) RETURNING id",
            datos["nombre"], datos.get("direccion", "")
        )
        return {"mensaje": "Edificio creado", "id": result["id"]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

 @app.patch("/edificios/{edificio_id}")
async def actualizar_edificio(edificio_id: int, datos: dict):
    db = await get_db()
    try:
        if "nombre" in datos:
            await db.execute(
                "UPDATE edificios SET nombre = $1 WHERE id = $2",
                datos["nombre"], edificio_id
            )
        if "activo" in datos:
            await db.execute(
                "UPDATE edificios SET activo = $1 WHERE id = $2",
                datos["activo"], edificio_id
            )
        return {"mensaje": "Edificio actualizado"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.get("/edificios/{edificio_id}/aulas")
async def aulas_por_edificio(edificio_id: int):
    db = await get_db()
    try:
        aulas = await db.fetch(
            "SELECT * FROM aulas WHERE edificio_id = $1 AND activa = TRUE",
            edificio_id
        )
        return [dict(a) for a in aulas]
    finally:
        await release_db(db)       