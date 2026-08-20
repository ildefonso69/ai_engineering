# Escalabilidad y alta concurrencia

Sesión 15 · Módulo 6

> **Nada de este documento está implementado.** Son límites reales de este
> sistema, medidos sobre este repositorio, con fichero y línea. Sirven para saber
> qué se rompería primero y en qué orden atacarlo — no para arreglarlo hoy.

---

## Los dos números

**Este sistema atiende 3 peticiones simultáneas y 5 operaciones de LLM.**

Y lo más importante: **ninguno de los dos está escrito en ninguna configuración.**

### 3 — los hilos de Puma

`business-backend/config/puma.rb:27-28`

```ruby
threads_count = ENV.fetch("RAILS_MAX_THREADS", 3)
threads threads_count, threads_count
```

No hay directiva `workers`: **un proceso, tres hilos**. Y cada estimación retiene
un hilo durante toda la llamada al servicio IA, porque Faraday sobre Net::HTTP es
bloqueante:

| Flujo | Timeout | Fichero |
|---|---|---|
| Chunking Lab | **600 s** | `rag/chunking_comparisons_controller.rb:12` |
| Generación / horas | 300 s | `rag/estimation_runs_controller.rb:12` |
| Resto de llamadas | 180 s | `initializers/estimator_ai.rb:3` |

**Tres ejecuciones del Chunking Lab dejan la aplicación entera sin servicio
durante diez minutos.** No es un umbral de carga, es un umbral de *tres personas*.

> **Trampa.** `puma.rb:8` documenta que `WEB_CONCURRENCY` controla los workers.
> La directiva `workers` no existe en el fichero: **ponerlo no hace nada**. La
> configuración documenta una perilla que no está conectada.

### 5 — el executor de asyncio

Todas las llamadas al LLM del servicio IA son síncronas envueltas en
`asyncio.to_thread` (**32 llamadas en 21 ficheros**: `generation/rag/estimator.py`,
`domain/graph/nodes.py`, `domain/graph/supervisor/*`, embeddings, idempotencia…).
`asyncio.to_thread` usa el executor **por defecto** del bucle, que Python
dimensiona a `min(32, cpu_count + 4)`.

**En una máquina de 1-2 vCPU eso son 5 o 6 hilos para todo el servicio.**

Una estimación fundamentada ocupa uno durante 30-120 s. A la sexta, las
peticiones **se encolan en silencio**: sin timeout, sin backpressure, sin 503.
El síntoma es que la latencia se dispara mientras la CPU está ociosa — que es
justo el patrón que más cuesta diagnosticar.

Peor: los abanicos no ayudan, compiten. `generation/rag/task_hours.py:227` hace
`asyncio.gather` sobre **todas** las tareas sin límite; una estructura de 40
tareas lanza 40 corrutinas peleando por 5 hilos.

---

## Lo que convierte "lento" en "caída"

El fallo más importante del sistema, y no es de rendimiento sino de diseño:

```
 3 estimaciones concurrentes
        │
        ▼
 los 3 hilos de Puma bloqueados
        │
        ▼
 /up deja de responder  ← lo sirve el MISMO pool de hilos
        │
        ▼
 el healthcheck falla (Dockerfile:114, retries 5 × 30 s)
        │
        ▼
 el contenedor se REINICIA a mitad de las estimaciones
        │
        ▼
 todo el trabajo en vuelo se pierde
```

Un sistema saturado se autodestruye en vez de degradarse. **Una sonda de salud
que comparte pool con el tráfico no mide la salud: la castiga.**

Cómo se evita en producción: servidor con pool separado para las sondas, o
`/up` servido por el proxy, o —lo correcto aquí— que las peticiones largas no
retengan un hilo.

---

## `async` no te salva del trabajo bloqueante

`async` gestiona **espera**, no **trabajo**. Dos ejemplos reales:

**1. Redis síncrono dentro del event loop.** `RuntimeRetrievalConfig` usa el
cliente *sync* de Redis (`foundation/llm/runtime_config.py:174`) y se llama desde
código async: **cuatro `HGET` bloqueantes por estimación**
(`generation/rag/estimator.py:226-227,262,305`). Y `GraphActivityLog` hace un
`LRANGE` bloqueante (`domain/graph/activity.py:206`) en **cada poll de progreso**,
que el wizard dispara **cada 1,5 segundos por ejecución abierta**. Cada uno para
todas las corrutinas del proceso.

**2. tiktoken en el bucle.** `generation/rag/estimator.py:260-261` codifica hasta
16.384 tokens de contexto sin ceder el control. Decenas de milisegundos de CPU
pura con el GIL cogido.

> El reranker, en cambio, **está bien hecho**: se ejecuta con `asyncio.to_thread`
> (`retrieval/pipeline.py:209`). Su problema es otro — la primera invocación
> descarga y carga ~470 MB de torch dentro de un hilo del executor **mientras
> mantiene un lock**, así que todo lo demás espera.

---

## Sin timeout no hay backpressure

No existe ningún timeout de servidor: ni `asyncio.wait_for` alrededor de un
pipeline, ni límite en uvicorn, ni middleware con deadline. Y las cuentas salen mal:

```
LLM_TIMEOUT = 30 s          (config.py:25)
max_retries = 6             (wrapper.py:295, hardcodeado)
   → 30 × 7 = 210 s por CADA llamada al LLM

Una estimación puede encadenar 5 llamadas
   → ~19 minutos de peor caso
```

Rails cuelga a los 180 s. **El servidor no se entera**: sigue reteniendo el hilo,
la conexión a la base de datos y el gasto en tokens hasta el final, para un
cliente que ya se fue. Multiplícalo por las 10 peticiones/minuto que el rate
limiter permite y el executor desaparece.

**Encolar sin límite no es resiliencia, es aplazar el fallo.** Un sistema honesto
rechaza (503 + `Retry-After`) cuando no puede atender.

---

## Qué escala horizontalmente y qué no

| Componente | ¿Réplicas? | Por qué |
|---|---|---|
| `caddy` | sí | Sin estado |
| `business-backend` | **casi** | Sesiones en cookie (bien), pero Active Storage en disco local rompe |
| `ai-service` | **no, todavía** | Ver abajo |
| `postgres` / `vector-db` | vertical + réplicas de lectura | Estado por definición |
| `redis` | no trivial | Requiere cluster |

### Lo que rompe al añadir la segunda réplica del servicio IA

| # | Estado | Dónde | Efecto |
|---|---|---|---|
| 1 | **Sesiones conversacionales en un `dict` en RAM** | `generation/conversation/store.py:23` | Creas la sesión en A, la usas en B → 404. **Su propio docstring lo avisa** |
| 2 | **Rate limiter en memoria** (sin `storage_uri`) | `api/rate_limiting.py:29` | Con N réplicas el límite efectivo es N × el configurado. Este cuesta dinero, no latencia |
| 3 | **`BackgroundTasks` son del proceso** | `api/routers/estimate_graph.py:245` | Un reinicio deja `/progress` diciendo `running` para siempre. No hay nadie que lo reintente |
| 4 | **Idempotencia y activity log degradan en silencio** | `idempotency.py:37`, `activity.py:155` | Si Redis no responde **en el instante del arranque**, ese proceso cae a un dict en memoria **para siempre**: un solo WARNING y a correr |
| 5 | **40 conexiones a Postgres por réplica** | `persistence/database.py:34,77` (15+15) + `graph/checkpointer.py:41` (10) | Ninguna configurada. 3 réplicas = 120 conexiones |
| 6 | **`alembic upgrade head` en cada arranque** | `docker-entrypoint.sh:18` | N réplicas migrando a la vez. `RUN_MIGRATIONS=false` en todas menos una |

Y en el backend de negocio:

| # | Estado | Dónde | Efecto |
|---|---|---|---|
| 7 | **Active Storage en disco local** | `environments/production.rb:25` | Con N réplicas, ~`(N-1)/N` de las descargas fallan. **Y hoy ya se pierden los adjuntos en cada redespliegue**, incluso con una sola |
| 8 | **El worker de Solid Queue no arranca nunca** | `puma.rb:37` | Está todo configurado —`queue.yml`, `bin/jobs`, el adaptador— pero `SOLID_QUEUE_IN_PUMA` no se define en ninguna parte. Un `perform_later` **se encolaría para siempre**. Es la trampa esperando a quien "arregle" el problema de los hilos moviendo las llamadas a un job |

---

## El techo real: las cuotas del proveedor

Puedes añadir servidores; no puedes añadir cuota. El límite de un sistema de IA
suele ser el **TPM/RPM** del proveedor, no tu CPU.

De ahí que la palanca más barata no sea la infraestructura sino **las cachés
CAG que ya existen**: la caché exacta y la semántica (`generation/cag/`) están en
Redis y son compartidas. Una petición servida desde caché no consume cuota, no
consume executor y responde en milisegundos.

**El *hit rate* de la caché es la métrica de coste del sistema.** Antes de tocar
nada más, mídela.

Lo demás que se hace en producción: reintento con *jitter* (no con espera fija,
o todos los clientes reintentan a la vez), modelo de *fallback* cuando el
primario está limitado, y una cola con prioridades para que el trabajo
interactivo no espere detrás de un lote.

---

## Cómo medir

- **p95 y p99, nunca la media.** Con colas, la media miente: la mitad de los
  usuarios puede estar esperando minutos mientras la media parece sana.
- **Saturación antes que utilización.** Lo que importa no es "cuánta CPU uso"
  sino "cuánto se espera para entrar": profundidad de cola, hilos ocupados,
  conexiones en uso.
- **Cardinalidad de las esperas.** El correlador `X-Request-ID` ya existe
  (`main.py`, middleware) y `LOGFIRE_TOKEN` está soportado y sin configurar.

---

## Si hubiera que arreglarlo, en este orden

Por relación entre impacto y esfuerzo:

1. **Sacar las llamadas largas del ciclo de petición.** El patrón ya existe en el
   grafo (202 + `/progress`); generalizarlo. Ojo con el punto 8: hay que arrancar
   el worker.
2. **Poner un timeout de servidor** y devolver 503 en vez de encolar.
3. **Subir el executor** (`loop.set_default_executor(...)` en el lifespan) o pasar
   a los clientes async del proveedor.
4. **Rate limiter en Redis**: `Limiter(..., storage_uri=settings.REDIS_URL)`. Un argumento.
5. **Pools explícitos** (`pool_size`, `max_overflow`) con la aritmética hecha
   contra `max_connections`.
6. **`SessionStore` a Redis** y **Active Storage a S3** — los dos bloqueantes de
   la replicación.
7. **Sondas con pool propio**, para que la saturación no provoque el reinicio.

Nada de esto es de la Sesión 15. Se enumera para que el salto siguiente esté
mapeado.
