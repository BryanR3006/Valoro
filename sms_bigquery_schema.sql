-- Esquemas verificados en BigQuery el 2026-07-29.
-- No ejecutar CREATE TABLE sobre tablas existentes: ya contienen estos campos.

CREATE TABLE IF NOT EXISTS `capable-arbor-209819.Temporal.SmsLog` (
  id INT64,
  telefono STRING NOT NULL,
  mensaje STRING NOT NULL,
  plantilla STRING,
  consulta_sql STRING,
  fecha_envio TIMESTAMP,
  resultado STRING,
  bulk_id STRING,
  error STRING,
  campana STRING,
  usuario STRING,
  fecha_programada TIMESTAMP,
  es_reenvio BOOL,
  programacion_id INT64,
  ip_origen STRING,
  fecha_creacion TIMESTAMP,
  fecha_actualizacion TIMESTAMP
);

CREATE TABLE IF NOT EXISTS `capable-arbor-209819.Temporal.ProgramacionSms` (
  id STRING,
  fecha_programada TIMESTAMP NOT NULL,
  consulta_sql STRING NOT NULL,
  plantilla STRING NOT NULL,
  fecha_ejecucion TIMESTAMP,
  estado STRING,
  total_destinatarios INT64,
  usuario STRING,
  periodo_duplicados_horas INT64,
  confirmar_duplicados BOOL,
  fecha_creacion TIMESTAMP,
  fecha_actualizacion TIMESTAMP
);
