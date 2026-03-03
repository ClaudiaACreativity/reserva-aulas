from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import date, time, datetime
from typing import Optional
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Reserva de Aulas")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://claudiaacreativity.github.io",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "null"
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

async def get_db():
    return await asyncpg.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )

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
        await db.close()

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
        await db.close()

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
        return {"mensaje": "Reserva creada", "id": str(result["id"])}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await db.close()

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
        return {"mensaje": "Reserva cancelada correctamente"}
    finally:
        await db.close()

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
        await db.close()

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
        await db.close()

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
        await db.close()

@app.get("/fechas-bloqueadas")
async def listar_fechas_bloqueadas():
    db = await get_db()
    try:
        fechas = await db.fetch(
            "SELECT * FROM fechas_bloqueadas ORDER BY fecha"
        )
        return [dict(f) for f in fechas]
    finally:
        await db.close()

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
        await db.close()

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
        await db.close()

# ===== GESTIÓN DE AULAS =====
class AulaCreate(BaseModel):
    nombre: str
    capacidad: int
    edificio: str

@app.post("/aulas")
async def crear_aula(aula: AulaCreate):
    db = await get_db()
    try:
        result = await db.fetchrow(
            """INSERT INTO aulas (nombre, capacidad, edificio)
               VALUES ($1, $2, $3) RETURNING id""",
            aula.nombre, aula.capacidad, aula.edificio
        )
        return {"mensaje": "Aula creada", "id": str(result["id"])}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await db.close()

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
        await db.close()

# ===== GESTIÓN DE USUARIOS =====
@app.get("/usuarios")
async def listar_usuarios():
    db = await get_db()
    try:
        usuarios = await db.fetch("SELECT * FROM usuarios ORDER BY nombre")
        return [dict(u) for u in usuarios]
    finally:
        await db.close()

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
        await db.close()

# ===== CONTROL DE RESERVAS =====
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
        await db.close()

# ===== GESTIÓN DE HORARIOS =====
@app.get("/horarios")
async def listar_horarios():
    db = await get_db()
    try:
        horarios = await db.fetch(
            "SELECT * FROM configuracion_horarios ORDER BY dia_semana"
        )
        return [dict(h) for h in horarios]
    finally:
        await db.close()

class HorarioUpdate(BaseModel):
    habilitado: bool
    hora_apertura: Optional[time] = None
    hora_cierre: Optional[time] = None

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
        await db.close()

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
        await db.close()