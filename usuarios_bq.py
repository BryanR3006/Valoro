"""
Usuarios compartidos entre todas las copias de la app (tabla en BigQuery).

Cada PC guarda los usuarios en su app.db (tabla usuarios) para que el ingreso
funcione aunque BigQuery falle; la tabla Temporal.Valoro_Usuarios es la fuente
común (Omnic, de QNT, usa otra: Temporal.Omnic_Usuarios). sincronizar() se
corre al arrancar, cada minuto (job usuarios_sync) y cuando alguien no logra
ingresar (puede ser un usuario creado en otro PC):

  - Fila de BigQuery más nueva que la local (o que no existe aquí) → se copia aquí.
  - Fila local más nueva que la de BigQuery (o que no existe allá) → se sube.
  - Fila marcada eliminado en BigQuery → se borra aquí.

"Más nueva" se decide con la columna actualizado (hora de Colombia), que se
marca con marcar_cambio() en cada cambio real del usuario (crear, perfil,
activo, clave). ultimo_ingreso no se sincroniza: es de cada PC y no debe
pisar cambios de otro PC. Se guarda el hash de la clave, nunca la clave.

En la copia de prueba (APP_COPIA_PRUEBA=1) solo se lee: no se escribe en
BigQuery.
"""
import logging
import time

TABLA = "capable-arbor-209819.Temporal.Valoro_Usuarios"
CAMPOS = ("usuario", "nombre", "clave_hash", "perfil", "activo", "debe_cambiar_clave",
          "fecha_creacion", "actualizado")

logger = logging.getLogger(__name__)
_ultima_por_ingreso = 0.0
_tabla_lista = False  # la tabla se revisa/crea una vez por arranque


def _cliente():
    from conexion_bigquery import get_bigquery_client
    return get_bigquery_client()


def _solo_lectura():
    try:
        from modo_prueba import es_copia_prueba
        return es_copia_prueba()
    except Exception:
        return False


def marcar_cambio(usuario):
    """Marca un cambio real del usuario (lo que debe viajar a los otros PC)."""
    from database import _ahora_colombia
    usuario.actualizado = _ahora_colombia()


def _asegurar_tabla(client):
    global _tabla_lista
    if _tabla_lista:
        return
    client.query(f"""
        CREATE TABLE IF NOT EXISTS `{TABLA}` (
          usuario STRING NOT NULL, nombre STRING, clave_hash STRING NOT NULL, perfil STRING NOT NULL,
          activo BOOL NOT NULL, debe_cambiar_clave BOOL NOT NULL, fecha_creacion DATETIME,
          actualizado DATETIME NOT NULL, eliminado BOOL NOT NULL
        )
        OPTIONS (description = "Usuarios de Valoro compartidos entre las copias de la app (usuarios_bq.py). Solo hashes de clave.")
    """).result()
    _tabla_lista = True


def _subir(client, filas, eliminado=False):
    """MERGE de filas (dicts con CAMPOS) en la tabla. eliminado=True marca el borrado."""
    from google.cloud import bigquery
    if not filas:
        return
    params, selects = [], []
    tipos = {"activo": "BOOL", "debe_cambiar_clave": "BOOL", "fecha_creacion": "DATETIME", "actualizado": "DATETIME"}
    for i, fila in enumerate(filas):
        columnas = []
        for campo in CAMPOS:
            nombre = f"{campo}_{i}"
            params.append(bigquery.ScalarQueryParameter(nombre, tipos.get(campo, "STRING"), fila.get(campo)))
            columnas.append(f"@{nombre} AS {campo}")
        selects.append("SELECT " + ", ".join(columnas))
    params.append(bigquery.ScalarQueryParameter("eliminado", "BOOL", eliminado))
    asignar = ", ".join(f"{c} = f.{c}" for c in CAMPOS if c != "usuario")
    client.query(f"""
        MERGE `{TABLA}` t
        USING ({" UNION ALL ".join(selects)}) f
        ON t.usuario = f.usuario
        WHEN MATCHED THEN UPDATE SET {asignar}, eliminado = @eliminado
        WHEN NOT MATCHED THEN INSERT ({", ".join(CAMPOS)}, eliminado)
             VALUES ({", ".join("f." + c for c in CAMPOS)}, @eliminado)
    """, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()


def _fila(usuario):
    return {c: getattr(usuario, c) for c in CAMPOS}


def marcar_eliminado(usuario):
    """Antes de borrar un usuario aquí: lo marca eliminado en BigQuery para que se
    borre también en los otros PC. Lanza excepción si no se pudo (no se borra)."""
    if _solo_lectura():
        raise RuntimeError("Copia de prueba: no se escribe en BigQuery.")
    from database import _ahora_colombia
    client = _cliente()
    _asegurar_tabla(client)
    fila = _fila(usuario)
    fila["actualizado"] = _ahora_colombia()
    _subir(client, [fila], eliminado=True)


def sincronizar(app, motivo="periódica"):
    """Trae lo nuevo de BigQuery y sube lo nuevo de aquí. Nunca lanza excepción."""
    from database import db, Usuario
    with app.app_context():
        try:
            client = _cliente()
            solo_leer = _solo_lectura()
            if not solo_leer:
                _asegurar_tabla(client)
            try:
                remotas = {r["usuario"]: dict(r) for r in client.query(f"SELECT * FROM `{TABLA}`").result()}
            except Exception:
                if not solo_leer:
                    raise
                remotas = {}  # copia de prueba sin tabla todavía
            locales = {u.usuario: u for u in Usuario.query.all()}
            traidos, borrados, subir = 0, 0, []
            for nombre, r in remotas.items():
                u = locales.get(nombre)
                if r["eliminado"]:
                    if u is not None and (u.actualizado is None or u.actualizado <= r["actualizado"]):
                        db.session.delete(u)
                        borrados += 1
                    continue
                if u is None:
                    db.session.add(Usuario(**{c: r[c] for c in CAMPOS}))
                    traidos += 1
                elif u.actualizado is None or u.actualizado < r["actualizado"]:
                    for c in CAMPOS:
                        setattr(u, c, r[c])
                    traidos += 1
                elif u.actualizado > r["actualizado"]:
                    subir.append(u)
            for nombre, u in locales.items():
                if nombre not in remotas:
                    if u.actualizado is None:
                        marcar_cambio(u)
                    subir.append(u)
            db.session.commit()
            if subir and not solo_leer:
                _subir(client, [_fila(u) for u in subir])
            if traidos or borrados or (subir and not solo_leer):
                logger.info(f"👥 Usuarios sincronizados con BigQuery ({motivo}): {traidos} traídos, "
                            f"{borrados} borrados, {0 if solo_leer else len(subir)} subidos")
        except Exception as e:
            db.session.rollback()
            logger.error(f"❌ No se pudieron sincronizar los usuarios con BigQuery ({motivo}): {e}")


def sincronizar_por_ingreso(app):
    """Al fallar un ingreso: trae usuarios nuevos de otros PC (máximo cada 15 s)."""
    global _ultima_por_ingreso
    if time.monotonic() - _ultima_por_ingreso < 15:
        return False
    _ultima_por_ingreso = time.monotonic()
    sincronizar(app, "ingreso fallido")
    return True
