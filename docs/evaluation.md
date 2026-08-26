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

---

## Hallazgos de la primera ejecución (26/08/2026)

La primera tanda contra la instancia encontró **tres defectos de producción**, y
ninguno de los tres era visible desde el smoke test. Vale la pena leerlos en orden,
porque cada uno tapaba al siguiente.

### 1. El modelo de fallback estaba mal escrito

`FALLBACK_MODEL=claude-haiku-4-5-2025` en el `.env` de la instancia — truncado; el
nombre real es `claude-haiku-4-5-20251001`. LiteLLM no resuelve el proveedor y
revienta **al construir el `LLMWrapper`**, así que fallaba *cualquier* endpoint que
llamara al modelo, con un 500 en 62 ms.

Llevaba roto desde el despliegue. El smoke test seguía verde porque el badge del
navbar consulta `/api/v1/config/models`, que no construye el wrapper.

### 2. El `.env.example` de la raíz había perdido `LLM_TIMEOUT`

Con el modelo arreglado, los cinco casos de estimación fallaban con un 502 tras
**91 segundos** exactos. La aritmética lo delata: `LLM_TIMEOUT` no estaba en la
plantilla de la raíz —la canónica para Docker desde la S15—, así que regía el
default de `config.py`, que es **30**. Con `LLM_RETRIES=2` son 3 intentos × 30 s =
90 s. Y la generación con `gpt-5`, esfuerzo alto y 64k de presupuesto tarda entre
dos y tres minutos.

30 s sobran para `gpt-4o-mini`; para un modelo de razonamiento son garantía de
fallo. `ai-service/.env.example` sí decía 600: la consolidación de la S15 perdió
la línea por el camino, y **ningún test lo detectó porque ningún test llama al
modelo**.

### 3. Lo de verdad interesante: la misma entrada da 566 o 82

Ya con todo funcionando, los cinco casos quedaron 3-5× por encima de lo esperado,
todos con `confidence: "low"`:

| Caso | Estimado | Esperado | Rango aceptable |
|---|---|---|---|
| ecommerce checkout | 566 | 75 | 45–120 |
| healthcare portal | 320 | 150 | 95–220 |
| logistics fleet | 507 | 100 | 62–150 |
| finance reporting | 347 | 90 | 55–140 |
| multi-componente | 417 | 190 | 120–280 |

`within_range_rate` **0%**, error medio **310 jornadas**. Pero una llamada
posterior al **mismo** `case-001` devolvió **82 jornadas** — dentro del rango — con
la conversión hecha correctamente línea a línea:

```
   5 d  Checkout flow orchestration     <- Estimated hours: 150
   6 d  Card payments integration       <- Estimated hours: 110
   2 d  Email provider integration      <- Estimated hours: 40
```

Misma entrada, 566 y 82. La causa está a la vista en
`app/generation/rag/prompt_builder.py`: **el prompt nunca dice cuántas horas tiene
una jornada.** Las fuentes hablan en `estimated_hours`, el campo del esquema se
llama `engineer_days`, y la regla 2 incluso pide copiar *"the component name and
its estimated hours"* como evidencia. La conversión queda implícita, así que a
veces el modelo divide entre 8 y a veces no — y una distribución bimodal como esa
es indistinguible de "el sistema estima alto" si solo miras una ejecución.

**Deliberadamente sin arreglar.** Es el material del directo: una línea de prompt
es justo el tipo de cambio que pide un A/B contra el golden set, y el número que
decide cuál gana es `within_range_rate`.

### 4. Y un cuarto, que encontró el propio dashboard

Con la instrumentación ya puesta, el primer panel salió con los tokens bien
contados y **`cost_usd: 0.0` en todas las filas**. El motivo: OpenAI responde con
la instantánea que sirvió de verdad —`gpt-4o-mini-2024-07-18`— mientras
`MODEL_COSTS` está indexada por el alias `gpt-4o-mini`. La búsqueda exacta fallaba
y la llamada se tarificaba a cero: un panel de costes afirmando que todo es
gratis.

Arreglado con `_price_for()`, que cae al **prefijo más largo** que coincida. Lo de
"más largo" es lo que importa: `gpt-4o-mini-2024-07-18` empieza también por
`gpt-4o`, y quedarse con el corto multiplicaría el precio por ~16. Un modelo
desconocido sigue valiendo cero, pero ahora deja un `model_not_in_pricing_table`
en el log, porque un cero silencioso es indistinguible de una llamada gratis.

### Lo que sí funcionó

- **La abstención, en las tres ejecuciones.** `case-006` nunca inventó una cifra:
  `confidence: "insufficient"`, sin número. La propiedad de seguridad se sostiene.
- **`citation_validity_rate` 100%.** Toda línea `grounded` citaba fuentes reales.
  El sistema no alucina *fuentes*; se equivoca en las *unidades*.
- **La latencia p95 es de 165 s**, muy por encima de los 180 s a los que Rails
  cuelga la llamada. Sobre eso, [escalabilidad](scalability.md).

### Una lectura de ejemplo del panel

`eval/reports/dashboard.html` está generado a partir de
`eval/reports/sample-production.log`, tráfico real (7 estimaciones, una búsqueda,
un 422 y un 401, más 10 sondas de salud):

```
  requests          10          <- las 10 sondas de /health NO están aquí
  error rate        20.0%       <- el 422 y el 401, provocados a propósito
  latency mean      0.91s
  latency p95       2.29s       <- la p95 es la que sufre la gente
  cost / request    $0.0001
  cost total        $0.0007     (4.060 tokens en 2 llamadas al modelo)
```

Diez peticiones y **solo dos llamadas al modelo**: las otras cinco estimaciones
las sirvió la caché exacta. Eso también es una señal — una petición cacheada sale
gratis y aparece con coste cero, y distinguirla de una que falló requiere mirar
la columna de estado, no la de coste.
