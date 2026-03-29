from fastapi import FastAPI, HTTPException
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

# ===== EMAIL =====
def enviar_email(destinatario: str, asunto: str, cuerpo: str):
    try:
        resend.Emails.send({
            "from": "Reserva de Espacios <onboarding@resend.dev>",
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

# ===== ENDPOINTS =====

@app.get("/")
async def inicio():
    return {"mensaje": "Sistema de Reserva de Espacios funcionando"}

@app.get("/espacios")
async def listar_espacios():
    db = await get_db()
    try:
        espacios = await db.fetch("SELECT * FROM aulas WHERE activa = TRUE")
        return [dict(e) for e in espacios]
    finally:
        await release_db(db)

@app.post("/espacios")
async def crear_espacio(espacio: EspacioCreate):
    db = await get_db()
    try:
        result = await db.fetchrow(
            """INSERT INTO aulas (nombre, capacidad, edificio_id)
               VALUES ($1, $2, $3) RETURNING id""",
            espacio.nombre, espacio.capacidad, espacio.edificio_id
        )
        return {"mensaje": "Espacio creado", "id": str(result["id"])}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await release_db(db)

@app.patch("/espacios/{espacio_id}")
async def toggle_espacio(espacio_id: str, datos: dict):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE aulas SET activa = $1 WHERE id = $2",
            datos["activa"], espacio_id
        )
        return {"mensaje": "Espacio actualizado"}
    finally:
        await release_db(db)

@app.get("/disponibilidad/{espacio_id}/{fecha}")
async def consultar_disponibilidad(espacio_id: str, fecha: date):
    db = await get_db()
    try:
        reservas = await db.fetch(
            "SELECT hora_inicio, hora_fin FROM reservas WHERE aula_id=$1 AND fecha=$2 AND estado='activa'",
            espacio_id, fecha
        )
        return [dict(r) for r in reservas]
    finally:
        await release_db(db)

@app.post("/reservas")
async def crear_reserva(reserva: ReservaCreate):
    db = await get_db()
    try:
        ahora = datetime.now()
        fecha_hora_inicio = datetime.combine(reserva.fecha, reserva.hora_inicio)
        if fecha_hora_inicio < ahora:
            raise HTTPException(
                status_code=400,
                detail="La fecha y hora de reserva debe ser posterior a la actual"
            )

        fecha_bloqueada = await db.fetchrow(
            "SELECT motivo FROM fechas_bloqueadas WHERE fecha = $1",
            reserva.fecha
        )
        if fecha_bloqueada:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede reservar en esa fecha: {fecha_bloqueada['motivo']}"
            )

        dia_semana = reserva.fecha.weekday()

        config = await db.fetchrow(
            "SELECT * FROM configuracion_horarios WHERE dia_semana = $1",
            dia_semana
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
            """INSERT INTO reservas (aula_id, usuario_id, fecha, hora_inicio, hora_fin)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            reserva.espacio_id, reserva.usuario_id, reserva.fecha,
            reserva.hora_inicio, reserva.hora_fin
        )

        usuario = await db.fetchrow(
            "SELECT email, nombre FROM usuarios WHERE id = $1",
            reserva.usuario_id
        )
        espacio = await db.fetchrow(
            "SELECT nombre FROM aulas WHERE id = $1",
            reserva.espacio_id
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
                <p style="margin-top:15px; color:#888">Sistema de Reserva de Espacios</p>
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
                <p style="color:#888">Sistema de Reserva de Espacios</p>
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
            usuario["email"], usuario["nombre"], usuario.get("rol", "usuario")
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
                      r.usuario_id, a.nombre as espacio_nombre
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
                      r.usuario_id, a.nombre as espacio_nombre,
                      u.nombre as usuario_nombre, u.email as usuario_email
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
                      a.id as espacio_id, a.nombre as espacio_nombre,
                      u.nombre as usuario_nombre
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
        fechas = await db.fetch("SELECT * FROM fechas_bloqueadas ORDER BY fecha")
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

@app.get("/edificios/{edificio_id}/espacios")
async def espacios_por_edificio(edificio_id: int):
    db = await get_db()
    try:
        espacios = await db.fetch(
            "SELECT * FROM aulas WHERE edificio_id = $1 AND activa = TRUE",
            edificio_id
        )
        return [dict(e) for e in espacios]
    finally:
        await release_db(db)

@app.get("/reservas/exportar")
async def exportar_reservas():
    db = await get_db()
    try:
        reservas = await db.fetch(
            """SELECT r.fecha, r.hora_inicio, r.hora_fin, r.estado,
                      a.nombre as espacio_nombre,
                      u.nombre as usuario_nombre, u.email as usuario_email
               FROM reservas r
               JOIN aulas a ON r.aula_id = a.id
               JOIN usuarios u ON r.usuario_id = u.id
               ORDER BY r.fecha DESC, r.hora_inicio DESC"""
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
