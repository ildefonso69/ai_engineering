# Evaluación y monitorización

> Sesión 16 · LLMOps. Tras la S15 el sistema está desplegado y el smoke test dice
> que **responde**. Este documento trata de la otra pregunta, la que el smoke test
> no contesta: si responde **bien**, y a qué precio.

Son dos instrumentos distintos y conviene no confundirlos:

| | Golden set + arnés | Dashboard |
|---|---|---|
| Qué mide | La calidad **cuando tú lanzas la medición** | Lo que pasa **en producción, todo el rato** |
| Analogía | La prueba de laboratorio | El monitor de constantes vitales |
| Cuándo corre | Deliberadamente, a mano | Continuamente, sin que nadie lo pida |
| Cuesta dinero | **Sí** | No |

---

## Por qué esto no está en CI

En el pipeline el modelo va doblado, porque **CI prueba nuestro código**. Esto es
lo contrario: **evalúa el modelo**, así que llama al modelo de verdad. Es lento,
cuesta dinero y no es determinista — las tres razones por las que no puede
bloquear un commit cualquiera.

Y una trampa relacionada, por si aparece la tentación: **`/health` no llama al
modelo y no debe hacerlo nunca**. Comprobar que el modelo responde con calidad es
trabajo del golden set y del dashboard, no de una sonda que se ejecuta cada 30
segundos.

## `eval/` no es `evals/`

En `ai-service/` conviven dos directorios parecidos y miden cosas distintas:

| Directorio | Sesión | Cómo llama al sistema | Qué puntúa |
|---|---|---|---|
| `evals/` | 4, 10, 11 | **En proceso**, contra el pipeline | Estimador conversacional, recuperación, métricas RAGAS |
| `eval/` | **16** | **Por HTTP**, contra el servicio desplegado | Calidad de la estimación de punta a punta, abstención, latencia y coste |

Uno pregunta *"¿es bueno el pipeline?"*; el otro, *"¿es bueno lo que hemos
desplegado?"*. No son la misma pregunta.

---

## El golden set — `ai-service/eval/golden_set.json`

Seis casos de referencia: transcripciones de reunión para las que **nosotros**, no
el modelo, hemos decidido cuál es una buena respuesta.

```json
{
  "id": "case-001-ecommerce-checkout",
  "transcript": "…",
  "expected_engineer_days": 75,
  "acceptable_range": [45, 120],
  "expect_abstention": false,
  "notes": "por qué ese número"
}
```

Tres decisiones que explican por qué el set es útil y no decorativo:

**1. Un rango, no una cifra.** Un caso pasa si la estimación cae **dentro del
intervalo aceptable**, no si acierta el número exacto. Una estimación es un
intervalo razonable; exigir igualdad mediría ruido.

**2. Los valores salen del corpus, no del ojo.** Están derivados de los proyectos
históricos realmente ingestados:

| Sector | Proyectos | Media (jornadas) | Rango |
|---|---|---|---|
| healthcare | 44 | 147 | 67–200 |
| government | 5 | 114 | 66–153 |
| education | 10 | 105 | 67–145 |
| industrial | 8 | 105 | 70–140 |
| media | 10 | 104 | 77–145 |
| logistics | 4 | 97 | 58–141 |
| finance | 8 | 84 | 22–139 |
| ecommerce | 8 | 75 | 9–100 |

Reproducible con:

```sql
with proj as (
  select metadata->>'budget_id' as bid, metadata->>'client_sector' as sector,
         sum((metadata->>'estimated_hours')::int) as hours
  from budget_chunks where chunk_type='historical_task' group by 1,2)
select sector, count(*), round(avg(hours)/8) from proj group by 1 order by 2 desc;
```

**3. Hay un caso que debe fallar en silencio.** `case-006` pide certificar
software de vuelo bajo DO-178C. El corpus cubre ocho sectores y la aviónica no es
ninguno: **no hay precedente**. La respuesta correcta no es un número, es
`confidence: "insufficient"` sin cifra. Eso mide **seguridad**, no acierto — y un
sistema que contesta ese caso no es más preciso, es peligroso.

---

## El arnés — `ai-service/eval/run_eval.py`

```bash
uv run python eval/run_eval.py --base-url http://localhost:8000
uv run python eval/run_eval.py --limit 1     # ensayo barato del propio arnés
uv run python eval/run_eval.py --case case-006-avionics-certification-no-precedent
```

Autenticación por **variable de entorno**, nunca en el código — las dos capas
independientes de la S15:

| Cabecera | Variable | Pregunta que responde |
|---|---|---|
| `X-Service-Token` | `AI_SERVICE_TOKEN` | ¿puedes hablar con este servicio? |
| `X-API-Key` | `ESTIMATE_API_KEY` | ¿puedes llamar a *este* router? |

**Dónde tiene que correr.** Desde la S15 el servicio IA no publica puertos: desde
fuera solo se llega al backend de negocio, por HTTPS. Así que el arnés se ejecuta
**dentro del perímetro**. No es un obstáculo a esquivar; es la frontera haciendo
su trabajo. Por eso `eval/` viaja dentro de la imagen, igual que `alembic/` y
`scripts/`.

```bash
H=<ip-de-la-instancia>; K=<clave.pem>
CO="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

ssh -i $K ubuntu@$H "cd /opt/estimator && $CO exec -T \
  -e AI_SERVICE_TOKEN -e ESTIMATE_API_KEY \
  ai-service python /app/eval/run_eval.py --base-url http://localhost:8000"
```

### Las métricas

| Métrica | Qué dice | Cómo leerla |
|---|---|---|
| `within_range_rate` | Fracción de estimaciones dentro del rango | La tasa de acierto |
| `mean_absolute_error` | Error medio en jornadas | *Cuánto* falla, no solo *si* falla |
| `abstention_correct` | ¿Se abstuvo donde no había datos? | Señal de **seguridad**. `n/a` significa que no se midió, no que esté bien |
| `mean_latency_ms` / `p95_latency_ms` | Latencia | La p95 es la que sufre la gente |
| `citation_validity_rate` | Toda línea `grounded` cita fuentes | Detecta desde fuera una rotura del contrato de grounding |

Cada ejecución se guarda en `eval/reports/report-<UTC>.json`. **Esa serie es el
entregable de verdad**: una ejecución suelta es una foto, la serie es lo que
permite ver si un cambio de prompt ha degradado la calidad.

El arnés sale con **código distinto de cero** si algún caso falla, para poder
enchufarlo como gate de regresión sin reescribirlo.

Dos detalles que ahorran un susto:

- **No envía `idempotency_key`.** Enviarlo devolvería la estimación cacheada: la
  petición parecería rápida y gratis, y la evaluación no mediría nada.
- **Se autolimita** por debajo del `10/minute` del endpoint, en vez de descubrir
  el límite como un 429 a mitad de una tanda que ya has pagado.

---

## El dashboard — `ai-service/eval/dashboard.py`

```bash
ssh -i $K ubuntu@$H "cd /opt/estimator && $CO logs --no-log-prefix ai-service" > ai.log
python3 ai-service/eval/dashboard.py --log-file ai.log \
    --html ai-service/eval/reports/dashboard.html
```

No hay agente, ni colector, ni servicio externo: lee el JSON estructurado que el
servicio ya escribe y se queda con **un solo evento**, `request_completed`, que el
middleware de `app/main.py` emite una vez por petición HTTP con latencia, tokens,
coste derivado y estado.

Que ese evento se emita en el **middleware** y no en cada router es lo que lo hace
completo: una petición que falla antes de llegar a un router también se cuenta, y
esa es la única forma de que la tasa de error sea creíble.

| Señal | Qué mirar |
|---|---|
| **Latencia** media y **p95** | La media esconde la cola; la p95 es la experiencia real |
| **Coste por petición** | Derivado de los tokens y la tabla de precios de `MODEL_COSTS` |
| **Tasa de error** | Proporción de respuestas no 2xx |

### El coste antes no se podía medir, y el motivo importa

Hasta la S16, `LLMWrapper.complete_structured` llamaba a
`chat.completions.create`, que devuelve solo el modelo ya parseado: **el `usage`
del proveedor se perdía**. Como toda la ruta RAG pasa por ahí, el servicio no
sabía cuántos tokens gastaba.

Y era peor que un hueco. `TurnObservation`, en el servicio de estimación, lleva
desde la S05 leyendo `meta["cost_usd"]` — una clave que nunca existía. Cada
estimación se persistía con **0 $**. No faltaba el dato: **el dato era falso**, que
es la razón por la que nadie notó nada.

El arreglo es `create_with_completion`, que devuelve el modelo *y* la respuesta
cruda, y un acumulador por petición en `app/foundation/observability/metrics.py`.

### Lo que el dashboard NO enseña, a propósito

- **Las sondas de salud.** `/health` se llama cada 30 s: contarla la convertiría en
  la mayoría de las filas, y la "p95 del servicio" sería en realidad la p95 de una
  sonda de vida.
- **El agente de la S12.** Conduce la Responses API a mano en vez de pasar por
  `LLMWrapper`, así que sus tokens no llegan al acumulador. Sus peticiones
  aparecen con coste 0: es un **punto ciego conocido**, no una llamada gratis.

---

## Cuánto cuesta

Una tanda completa (6 casos, `gpt-5` con razonamiento alto) ronda **1–3 USD**. Es
un gasto pequeño y deliberado: es el ejercicio. Cada caso tarda 1–3 minutos, de
ahí que el arnés use un timeout de 600 s por petición.
