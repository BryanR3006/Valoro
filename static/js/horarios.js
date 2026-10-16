/* Horarios de las programaciones SMS recurrentes (igual que services/sms_horarios.py):
   una vez al día a la hora de inicio, o cada N minutos hasta la hora "Hasta las". */
(function () {
  'use strict';

  // Opciones de "Repetir en el día" (minutos; vacío = una vez al día).
  const INTERVALOS = [
    ['', 'Una vez al día'], ['30', 'Cada 30 min'], ['60', 'Cada 1 hora'], ['90', 'Cada 1 h 30'],
    ['120', 'Cada 2 horas'], ['180', 'Cada 3 horas'], ['240', 'Cada 4 horas']
  ];

  function minutos(hora) {
    const m = String(hora || '').match(/^(\d{1,2}):(\d{2})/);
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
  }
  function hhmm(min) { return `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`; }

  // Horas del día en que sale: ['08:00', '10:00', ...]. Vacío si el horario no es válido.
  function horasDelDia(inicio, intervalo, fin) {
    const a = minutos(inicio);
    if (a === null) return [];
    if (!Number(intervalo)) return [hhmm(a)];
    const b = minutos(fin);
    if (b === null || b <= a) return [];
    const horas = [];
    for (let m = a; m <= b; m += Number(intervalo)) horas.push(hhmm(m));
    return horas;
  }

  function textoIntervalo(min) {
    const h = Math.floor(min / 60), r = min % 60;
    return h ? `${h} h${r ? ' ' + String(r).padStart(2, '0') : ''}` : `${r} min`;
  }

  // "Todos los días a las 08:00" o "Todos los días 08:00 a 18:30, cada 2 h".
  function texto(inicio, intervalo, fin) {
    const ini = String(inicio || '—').slice(0, 5);
    if (!Number(intervalo) || !fin) return `Todos los días a las ${ini}`;
    return `Todos los días ${ini} a ${String(fin).slice(0, 5)}, cada ${textoIntervalo(Number(intervalo))}`;
  }

  // Llena un <select> con las opciones de intervalo.
  function llenarSelect(select) {
    select.innerHTML = INTERVALOS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('');
  }

  window.ValoroHorarios = { INTERVALOS, horasDelDia, texto, textoIntervalo, llenarSelect };
})();
