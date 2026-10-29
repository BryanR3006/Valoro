"""
Horarios de las programaciones SMS recurrentes.

Una recurrente sale todos los días a hora_inicio. Con intervalo (minutos) se
repite en el día cada ese tiempo, sin pasar de hora_fin. Ejemplo: 08:00 cada
120 min hasta 18:30 → 08:00, 10:00, 12:00, 14:00, 16:00, 18:00.

Sin Flask, base de datos ni red: se puede probar con fechas cualquiera.
"""
from datetime import datetime, timedelta

# Cada cuánto se puede repetir en el día (minutos).
INTERVALOS_PERMITIDOS = (30, 60, 90, 120, 180, 240)


def _minutos(hora):
    """'HH:MM' → minutos desde la medianoche. ValueError si no es una hora válida."""
    hh, mm = str(hora or "").strip().split(":")[:2]
    hh, mm = int(hh), int(mm)
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"Hora inválida: {hora}")
    return hh * 60 + mm


def validar_horario(hora_inicio, intervalo=None, hora_fin=None):
    """Mensaje de error del horario, o None si está bien."""
    try:
        inicio = _minutos(hora_inicio)
    except (ValueError, TypeError):
        return "Hora de envío inválida. Use HH:MM."
    if not intervalo:
        return None
    if intervalo not in INTERVALOS_PERMITIDOS:
        return "El intervalo debe ser de 30 min, 1 h, 1 h 30, 2 h, 3 h o 4 h."
    if not hora_fin:
        return "Con envío repetido en el día, indique hasta qué hora (Hasta las)."
    try:
        fin = _minutos(hora_fin)
    except (ValueError, TypeError):
        return "Hora 'Hasta las' inválida. Use HH:MM."
    if fin <= inicio:
        return "La hora 'Hasta las' debe ser después de la hora de inicio."
    return None


def horas_del_dia(hora_inicio, intervalo=None, hora_fin=None, ventana=None):
    """
    Horas ('HH:MM') en que sale en un día: solo hora_inicio, o cada intervalo
    hasta hora_fin. ventana: (desde, hasta) del horario de envío; los turnos
    fuera de él no salen y no se cuentan.
    """
    inicio = _minutos(hora_inicio)
    if not intervalo or not hora_fin:
        minutos = [inicio]
    else:
        minutos = range(inicio, _minutos(hora_fin) + 1, int(intervalo))
    if ventana:
        desde, hasta = (_minutos(h) for h in ventana)
        minutos = [m for m in minutos if desde <= m <= hasta]
    return [f"{m // 60:02d}:{m % 60:02d}" for m in minutos]


# Horario de envío: un envío solo empieza entre desde y hasta (hasta incluido:
# el turno de las 18:00 sale). El último paso (lotes a Infobip, campaña de
# email) se revisa otra vez con este margen: termina lo que empezó a tiempo,
# pero nada sale horas después (p. ej. si el equipo se suspendió a mitad).
MARGEN_FIN_ENVIO_MIN = 15


def fuera_de_horario(ahora, desde, hasta, margen_min=0):
    """
    Mensaje claro si ahora (hora de Colombia) está fuera del horario de envío
    [desde, hasta]; None si se puede enviar. margen_min: minutos de gracia
    después de hasta.
    """
    minuto = ahora.hour * 60 + ahora.minute
    if _minutos(desde) <= minuto <= _minutos(hasta) + margen_min:
        return None
    return (f"Fuera del horario de envío ({desde} a {hasta}): son las {ahora:%H:%M} y "
            "no se envía nada.")


def error_hora_de_envio(hora, desde, hasta, campo="La hora de envío"):
    """Mensaje si 'HH:MM' queda fuera del horario de envío (al crear o editar una programación); None si está bien."""
    if _minutos(desde) <= _minutos(hora) <= _minutos(hasta):
        return None
    return (f"{campo} ({hora}) debe estar entre las {desde} y las {hasta}: fuera de ese horario "
            "no se envía nada.")


def turno_a_enviar(ahora, ultima_ejecucion, hora_inicio, intervalo=None, hora_fin=None, ventana=None):
    """
    Turno (datetime) que toca enviar ahora, o None si no toca.

    ahora y ultima_ejecucion en la misma zona (hora de Colombia).
    - Una vez al día (sin intervalo): si ya pasó hora_inicio y hoy no se ha
      enviado. Si la app estuvo apagada a esa hora, sale cuando vuelva (como antes).
    - Con intervalo: el último turno que ya llegó, si no se envió todavía, no
      pasó hora_fin y no va muy tarde (más de la mitad del intervalo, mínimo
      15 min): un turno perdido se salta para no mandar dos seguidos.
    ventana: (desde, hasta) del horario de envío; los turnos fuera de él no salen.
    """
    hoy = ahora.date()
    turnos = [datetime.combine(hoy, datetime.min.time()).replace(hour=int(h[:2]), minute=int(h[3:]))
              for h in horas_del_dia(hora_inicio, intervalo, hora_fin, ventana)]
    if ahora.tzinfo is not None:
        turnos = [ahora.tzinfo.localize(t) if hasattr(ahora.tzinfo, "localize") else t.replace(tzinfo=ahora.tzinfo)
                  for t in turnos]

    llegados = [t for t in turnos if t <= ahora]
    if not llegados:
        return None
    turno = llegados[-1]
    if ultima_ejecucion is not None and ultima_ejecucion >= turno:
        return None

    if intervalo and hora_fin:
        if ahora > turnos[0].replace(hour=_minutos(hora_fin) // 60, minute=_minutos(hora_fin) % 60):
            return None
        if ahora - turno > timedelta(minutes=max(15, int(intervalo) // 2)):
            return None
    return turno


# Un turno sin ejecución se da por perdido pasado este tiempo (los jobs revisan
# cada 1 o 10 minutos y una recurrente con intervalo salta un turno muy tarde).
MARGEN_TURNO_MIN = 20


def turnos_del_dia(horas, ejecuciones, ahora, desde=None, margen_min=MARGEN_TURNO_MIN):
    """
    Estado de cada turno del día para Inicio (SMS y email).

    horas: turnos esperados hoy ('HH:MM'). ejecuciones: dicts de las
    ejecuciones de hoy (EjecucionProgramacion.a_dict(): turno 'HH:MM',
    resultado enviado / sin_datos / error, ...), en orden. ahora y desde:
    datetime sin zona (hora de Colombia); los turnos antes de desde (cuando se
    creó la programación o empezó el registro) sin ejecución no se muestran.
    Un turno sin ejecución queda 'pendiente' (aún no llega o va dentro del
    margen) o 'no_corrio'. Las ejecuciones fuera de los turnos esperados
    (reintentos) también van. Ordenado por hora.
    """
    por_turno = {}
    for e in ejecuciones:
        por_turno.setdefault(e.get("turno") or e.get("a_las"), []).append(e)
    resultado = []
    for hora in horas:
        if hora in por_turno:
            resultado.extend(por_turno.pop(hora))
            continue
        momento = ahora.replace(hour=int(hora[:2]), minute=int(hora[3:5]), second=0, microsecond=0)
        if momento > ahora or ahora - momento < timedelta(minutes=margen_min):
            resultado.append({"turno": hora, "resultado": "pendiente"})
        elif desde is None or momento >= desde:
            resultado.append({"turno": hora, "resultado": "no_corrio"})
    for resto in por_turno.values():
        resultado.extend(resto)
    return sorted(resultado, key=lambda t: t.get("turno") or t.get("a_las") or "")


def texto_pendientes(programaciones, maximo=2):
    """
    Línea de la consola con los turnos que faltan hoy, a partir de las filas de
    Inicio (canal, campana, turnos con su resultado): "⏳ Pendientes hoy: 6
    turnos (4 SMS, 2 email) · próximo 16:00: SMS 'MOM5 …', email 'MOM6 …'".
    Los turnos que pasaron sin correr (no_corrio) van al final como aviso.
    """
    def canal(c):
        return "SMS" if c == "sms" else "email"

    pendientes, sin_correr = [], 0
    for prog in programaciones:
        for turno in prog.get("turnos") or []:
            if turno.get("resultado") == "pendiente":
                pendientes.append((turno.get("turno") or "", prog.get("canal"),
                                   prog.get("campana") or str(prog.get("id"))))
            elif turno.get("resultado") == "no_corrio":
                sin_correr += 1
    aviso = f" · ⚠️ {sin_correr} turnos de hoy no corrieron (ver Inicio)" if sin_correr else ""
    if not pendientes:
        return "⏳ No quedan programaciones pendientes hoy." + aviso
    por_canal = {c: sum(1 for _, k, _ in pendientes if k == c) for c in ("sms", "email")}
    proximo = min(hora for hora, _, _ in pendientes)
    a_esa_hora = [f"{canal(c)} '{nombre}'" for hora, c, nombre in pendientes if hora == proximo]
    siguientes = ", ".join(a_esa_hora[:maximo])
    if len(a_esa_hora) > maximo:
        siguientes += f" y {len(a_esa_hora) - maximo} más"
    canales = ", ".join(f"{n} {canal(c)}" for c, n in por_canal.items() if n)
    return f"⏳ Pendientes hoy: {len(pendientes)} turnos ({canales}) · próximo {proximo}: {siguientes}{aviso}"
