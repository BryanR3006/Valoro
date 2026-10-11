# Valoro — Envíos de SMS y email

Aplicación web en **Flask + SQLite (Flask-SQLAlchemy) + APScheduler** para enviar
**SMS (Infobip)** y **email (API esmsv.com)** a bases que salen de una consulta de
**Google BigQuery** (proyecto `capable-arbor-209819`) o, para SMS, de un Excel.
Envíos inmediatos o programados (simples o recurrentes), plantillas guardadas,
listas negras y dashboard de SMS. Código, logs e interfaz en español.

## Instalar y correr

```bash
pip install -r requirements.txt
python app.py              # http://localhost:5000 (la variable PORT cambia el puerto)
```

La app corre con `debug=False, use_reloader=False` a propósito (un solo proceso,
un solo scheduler). **Después de editar un `.py` hay que reiniciarla a mano.**

## Credenciales (no van en git)

| Archivo | Para qué |
|---|---|
| `config/google_key.json` | Cuenta de servicio de BigQuery (o `gcloud` ADC). La lee `conexion_bigquery.py`. |
| `config.json` (raíz) | Claves de Infobip y de la API de email. Se crea copiando `config.example.json`. |

Llaves de `config.json`:

```jsonc
{
  "general_parameters": {                 // se editan en Configuración > Parámetros generales
    "console_message_interval_seconds": 60,
    "log_retention_hours": 72
  },
  "infobip": {                            // SMS
    "api_key": "",                        // obligatorio para enviar
    "base_url": "",                       // obligatorio: https://<cuenta>.api.infobip.com
    "sender_id": "",                      // obligatorio: remitente de los SMS
    "batch_size": 500,                    // opcional: SMS por lote (máx. 500)
    "max_workers": 3,                     // opcional: lotes en paralelo (máx. 5)
    "senuelos": []                        // opcional: números de control que reciben cada envío
                                          // [{"telefono": "300...", "nombre": "...", "link": "https://..."}]
  },
  "email": {                              // email
    "api_key": "",                        // obligatorio para enviar
    "from_email": "",                     // obligatorio: remitente
    "from_alias": ""                      // opcional: nombre del remitente (por defecto "Valoro")
    // "field_mapping": {"columna": id}   // opcional: columna -> campo personalizado de la cuenta
  }
}
```

`config.json`, `config/google_key.json`, `app.db` y los logs están en `.gitignore`.
Nunca escriba claves en el código.

## Flujo de un envío

1. **Consulta** SQL (solo `SELECT`/`WITH`) o **Excel** (solo SMS).
2. **Validación en BigQuery** (*dry run*, `consulta_segura.py`); si BigQuery la rechaza, la pantalla muestra su mensaje.
3. **Mensaje**: con el *Constructor de mensaje* (Plantillas SMS por operador/categoría/tipo) o escrito a mano, con `{{Columna}}`.
4. **Acortador**: Infobip acorta los links de los SMS (`urlOptions.shortenUrl`).
5. **Listas negras** (`listas_negras.py`) y duplicados del día. Si la lista negra no carga, no se envía.
6. **Enviar ya** o **programar** (simple: fecha y hora; recurrente: todos los días a una hora, hasta una fecha fin).

Las programaciones de SMS se ven, editan, cancelan y reintentan en *SMS > Programaciones SMS*;
las de email en la pestaña *Programados* de *Email > Enviar Email*. Si una ejecución
falla queda en estado **error** con el mensaje del error.

## Listas negras

`listas_negras.py` tiene las tablas en un solo lugar:

- `LISTA_NEGRA_SMS` → `Tablas_Reporteria.Telefonos_Tutela` (teléfonos)
- `LISTA_NEGRA_EMAIL` → `Tablas_Reporteria.Email_Tutela` (correos)

## Logs

- `logs/sms.log` y `logs/email.log`: eventos y errores de cada canal (rotan a los 5 MB).
- `app_log.txt`: todo lo de la consola. `execution_log.txt`: tareas (lo lee la actividad en vivo de Inicio).

## Tablas en BigQuery

- `Temporal.SmsLog` (cada SMS), `Temporal.SmsInfobip` (copia de Infobip para el dashboard), `Temporal.EmailLog`, `Temporal.ProgramacionEmail_Valoro`. Esquemas de SMS en `sms_bigquery_schema.sql`.
- Las programaciones de email de Valoro van en su propia tabla `Temporal.ProgramacionEmail_Valoro` (la de QNT es otra, para que una app no envíe las de la otra). Crearla una vez:

  ```sql
  CREATE TABLE IF NOT EXISTS `capable-arbor-209819.Temporal.ProgramacionEmail_Valoro` (
    id STRING, fecha_programada TIMESTAMP, consulta_sql STRING, plantilla STRING, asunto STRING,
    campana STRING, estado STRING, usuario STRING, reply_email STRING,
    fecha_creacion TIMESTAMP, fecha_actualizacion TIMESTAMP,
    tipo STRING,          -- simple o recurrente
    ultimo_error STRING   -- mensaje del fallo
  );
  ```

- Email recurrente: sale todos los días a la hora de `fecha_programada`. Después de cada envío queda `pendiente` para el día siguiente. Si falla queda en `error` y se reactiva al editarla.

## Estructura

| Archivo | Qué hace |
|---|---|
| `app.py` | Rutas web y jobs del scheduler (SMS, email, dashboard SMS). |
| `backend.py` | Crea la app Flask, la base SQLite, los logs y el scheduler (`America/Bogota`). `APP_NOMBRE` = nombre en la interfaz. |
| `database.py` | Modelos: `ProgramacionSms`, `MensajeOperacion` (plantillas SMS), `PlantillaEmail`. Columnas nuevas: `_ensure_programacion_sms_columns()`. |
| `services/sms_service.py` | Envío Infobip, variables, lista negra, duplicados, SmsLog, polling de estados. |
| `services/sms_excel.py`, `services/sms_exclusiones.py` | Base SMS desde Excel; exclusiones del equipo de SMS. |
| `services/email_service.py`, `services/email_client.py` | Envío de email (contactos, lista, campaña), lista negra, EmailLog, plantillas. |
| `dashboard_sms.py`, `infobip_sync.py`, `infobip_logs.py` | Dashboard SMS y copia de los SMS de Infobip a BigQuery. |
| `bigquery.py`, `conexion_bigquery.py`, `consulta_segura.py` | Lectura de bases en BigQuery, credenciales, validación de consultas. |
| `listas_negras.py` | Tablas de lista negra. |
| `general_params.py` | Parámetros generales en `config.json`. |
| `modo_prueba.py` | Copia de prueba (`APP_COPIA_PRUEBA=1`, otra carpeta, puerto distinto de 5000). |
| `templates/`, `static/css/valoro.css` | Interfaz (AdminLTE + diseño Valoro). Logo: `static/img/logo-valoro.png`. |
