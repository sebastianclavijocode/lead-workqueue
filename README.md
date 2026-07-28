# Work Queue — Gestión de Leads Fríos

Prototipo funcional (Streamlit + SQLite) de la herramienta de "cola de trabajo" descrita en el
brief: el asesor nunca navega la base de datos, solo tipifica un lead a la vez.

> Este prototipo prioriza velocidad de validación. La arquitectura de producción recomendada
> (Next.js + NestJS + PostgreSQL) se describe abajo para cuando el producto esté validado.

## Cómo correrlo

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Usuarios demo (creados automáticamente en el primer arranque):

| Usuario     | Contraseña  | Rol         |
|-------------|-------------|-------------|
| admin       | admin123    | admin       |
| supervisor  | super123    | supervisor  |
| asesor1     | asesor123   | asesor      |
| asesor2     | asesor123   | asesor      |

## Qué implementa este prototipo

- **Importación** de CSV/Excel con mapeo de columnas (Nombre, Teléfono, Carrera, Años de
  experiencia, Dolor, Monto económico) → creación de leads + campaña.
- **Asignación** automática (round robin) o manual.
- **Work Queue del asesor**: un solo lead visible, tipificación obligatoria antes de continuar,
  campos condicionales con selector de fecha/hora tipo calendario (ej. "Llamar después" exige
  fecha/hora/observaciones), sin listado, sin buscador, sin exportar, sin poder saltar registros.
  El ID del lead nunca aparece en la URL.
- **La pantalla no avanza sola**: mientras un lead está "en progreso" (abierto), cambiar la
  tipificación u otro campo sin presionar "Guardar y siguiente" ya NO salta al próximo lead —
  antes era un bug porque la cola solo miraba leads "pending" y al abrir uno se marcaba
  "in_progress", desapareciendo de esa consulta en el siguiente rerender.
- **"No contesta"**: el lead vuelve a la cola en una **posición aleatoria** (no al final ni de
  inmediato) para reintentar más tarde. Al tercer "No contesta" se cierra automáticamente como
  gestionado.
- **"Llamar después"**: abre selector de fecha y hora (calendario/reloj nativos), el lead queda en
  `pending` para la fecha programada, y si el asesor configuró su correo se envía una notificación
  de confirmación + un recordatorio automático cuando la fecha/hora se cumple (ver sección de
  correo más abajo).
- **Correo del asesor**: sección en el panel del asesor para guardar/actualizar su email, usado
  para los recordatorios de seguimiento.
- **Tipificaciones configurables**: cada una define si cierra el lead o si vuelve a la cola
  (ej. "No contesta" y "Llamar después" no cierran de inmediato; "Venta" sí).
- **Auditoría**: se registra login, apertura de cada lead, guardado de gestión, asignación y
  reasignación (tabla `audit_log`).
- **Dashboards** para admin (cargados/pendientes/gestionados/ventas/**citas agendadas**,
  productividad por asesor, citas por asesor, leads por campaña) y supervisor (progreso por
  asesor, ventas, citas, historial reciente, reasignación).
- Permisos validados en la capa de datos/consultas, no solo ocultando botones en la UI: un asesor
  solo puede ver/gestionar leads donde `assigned_to == user.id`.

### Configurar el envío real de correos (opcional)

Sin configuración, los recordatorios de "Llamar después" simplemente no se envían (el lead sigue
funcionando normalmente, solo no llega el email). Para activarlo, crea el archivo
`.streamlit/secrets.toml` dentro de la carpeta del proyecto:

```toml
[smtp]
host = "smtp.gmail.com"
port = 587
user = "tu_correo@gmail.com"
password = "tu_contraseña_de_aplicación"
from = "tu_correo@gmail.com"
```

> Importante: el recordatorio "a la hora exacta" solo se revisa cada vez que el asesor abre o
> recarga la app (no hay un proceso corriendo en segundo plano). Para un envío puntual real en
> producción se necesita un scheduler independiente (cron, APScheduler o una cola tipo BullMQ) —
> ver sección de arquitectura de producción más abajo.

### Cambio de esquema — importante si ya tenías el prototipo corriendo

Los campos "Empresa"/"Ciudad" fueron reemplazados por "Carrera"/"Años de experiencia"/"Dolor"/
"Monto económico", y se agregaron columnas nuevas (`sort_key`, `no_contesta_count`, `email`,
`reminder_sent`). Si ya habías corrido la app antes, **borra el archivo `data/workqueue.db`** antes
de volver a ejecutar `streamlit run app.py`, para que se cree con el esquema nuevo (se recrean
también los usuarios y tipificaciones demo).

## Riesgos y mejoras de producto detectadas

1. **Concurrencia de reasignación**: si un supervisor reasigna un lead mientras el asesor lo tiene
   abierto, hay una condición de carrera. En producción: bloquear el lead (`locked_by`,
   `locked_at`) al abrirlo y liberar al guardar o por timeout.
2. **"Llamar después" sin recordatorio activo**: hoy el lead solo vuelve a la cola; falta un
   scheduler que lo traiga de vuelta *en la fecha/hora exacta* (no antes). En producción: un job
   (cron o cola tipo BullMQ) que mueva `next_follow_up <= now()` a `pending`.
2b. Mientras no exista ese job, un lead con seguimiento futuro puede volver a aparecer antes de
    tiempo; el prototipo lo deja pendiente de inmediato — anotado como deuda técnica conocida.
3. **Exportación por "screenshot humano"**: ningún control técnico evita que el asesor fotografíe
   la pantalla o copie el teléfono a mano. Se mitiga con límites de proceso (auditoría + alertas de
   volumen inusual de gestiones/hora), no solo con controles de software.
4. **Un solo lead en pantalla no impide anotar datos afuera del sistema**. Ninguna arquitectura
   técnica resuelve esto al 100%; se documenta como riesgo aceptado del modelo de negocio.
5. **Multi-tenant**: si RI u otros negocios usarán esto para varios clientes, conviene introducir
   `tenant_id` desde ya en todas las tablas para evitar una migración dolorosa después.

## Arquitectura recomendada para producción

```
Frontend:   Next.js (App Router) + React Query — SPA-like, sin exponer IDs de lead en la URL
Backend:    NestJS (REST) — guards de rol a nivel de endpoint, no solo de UI
DB:         PostgreSQL — mismas tablas que db.py, migrar con Prisma
Auth:       JWT + refresh token, roles en el payload, validado en cada endpoint
Colas:      BullMQ/Redis — para SLA, recordatorios de "llamar después", distribución automática
Auditoría:  tabla append-only + índice por lead_id y user_id para trazabilidad
```

### Modelo de datos (equivalente al de este prototipo)

- `users(id, username, password_hash, name, role, active)`
- `campaigns(id, name, created_at)`
- `leads(id, campaign_id, name, phone, company, city, extra_info jsonb, status, assigned_to,
  next_follow_up, created_at)`
- `tipificaciones(id, name, required_fields jsonb, is_final)`
- `gestiones(id, lead_id, user_id, tipificacion_id, notes, extra_data jsonb, created_at)`
- `audit_log(id, user_id, action, lead_id, detail, timestamp)`

Índices clave: `leads(assigned_to, status)`, `leads(next_follow_up)`, `audit_log(lead_id)`.

### Reglas de permisos (deben validarse en backend, no solo en frontend)

- Un asesor solo puede leer/escribir sobre leads donde `assigned_to = current_user.id` **y**
  `status != 'done'`. Cualquier otro `lead_id` en la URL/API responde 403, no 404 (para no filtrar
  existencia).
- Endpoints de listado completo, búsqueda y exportación solo existen para roles `admin`/`supervisor`.
- Todo cambio de estado y toda apertura de lead se audita server-side, nunca confiando en eventos
  del cliente.

## Roadmap (según el brief)

WhatsApp Business API · Telefonía VoIP/marcador automático · SMS · Email · Automatizaciones ·
IA para resumir llamadas · IA para sugerir tipificación · Distribución automática · SLA ·
Prioridades · Reglas de negocio.

La tabla `tipificaciones` ya soporta campos dinámicos (`required_fields` como JSON), y
`extra_info`/`extra_data` como JSON permiten agregar campos sin migraciones — buena base para
estas extensiones futuras.

## Publicar en GitHub

Este directorio ya es un repositorio git local. Para subirlo:

```bash
git add -A
git commit -m "Prototipo Work Queue de leads fríos"
git remote add origin https://github.com/<tu-usuario>/<tu-repo>.git
git branch -M main
git push -u origin main
```
