CREATE TABLE fechas_bloqueadas (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL UNIQUE,
    motivo VARCHAR(255) NOT NULL,
    creado_en TIMESTAMP DEFAULT NOW()
);