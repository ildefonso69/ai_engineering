# Observabilidad: qué se vigila y cómo se lee

> Sesión 16 · LLMOps. Complemento de [Evaluación](evaluation.md): aquello mide la
> calidad **cuando tú lanzas la medición**; esto mide lo que pasa **todo el rato**.

Un sistema de IA en producción tiene tres preguntas abiertas que un healthcheck no
contesta:

1. ¿Está respondiendo? → eso sí lo dice `/health` y `/health/ready`
2. ¿Está respondiendo **bien**? → golden set + regression gate
3. ¿A qué **precio** y con qué **latencia**? → esto

---

## Las cuatro señales

Todas salen de **un solo evento**, `request_completed`, que el middleware de
`ai-service/app/main.py` emite una vez por petición HTTP.

| Señal | Qué es | Qué mirar |
|---|---|---|
| **Latencia** (media y p95) | Tiempo de respuesta | La p95, no la media: la media esconde la cola y la cola es lo que sufre la gente |
| **Coste por petición** | Tokens × precio del modelo | Su **tendencia**. Un salto sin cambio de tráfico es un cambio de prompt o de modelo |
| **Tasa de error** | Respuestas no 2xx | Junto a la de abstención, o confundirás dos cosas opuestas |
| **Tasa de abstención** | Peticiones donde el sistema declinó | Que suba es el sistema siendo **prudente**; que suba la de error es el sistema **roto** |

Que el evento se emita en el **middleware** y no en cada router es lo que lo hace
completo: una petición que falla antes de llegar a un router también se cuenta, y
esa es la única forma de que la tasa de error sea creíble.

**Las sondas de salud están excluidas.** `/health` se llama cada 30 s: contarla la
convertiría en la mayoría de las filas, y la "p95 del servicio" sería en realidad
la p95 de una sonda de vida.

---

## Coste por tramo: la tabla que zanja discusiones

`log_stage` (`app/generation/rag/observability.py`) envuelve cada etapa del
pipeline RAG y ahora emite también los tokens y el dinero gastados **dentro** de
esa etapa, como delta del acumulador por petición.

```
  stage                   runs     mean   $ total
  generation                12   91.20s  $ 3.4800
  reformulation             12    1.10s  $ 0.0900
  bounds_guardrail          12    0.00s  $ 0.0000
  retrieval                 12    0.12s  $ 0.0000
```

*"Esta estimación costó 0,31 $"* no te dice qué hacer. *"La generación son 0,29 $
de esos"* sí — y es el número que decide si merece la pena un modelo más barato
para los pasos pequeños. Casi nunca lo merece, y el dato lo dice antes de que
pierdas una semana.

Un tramo sin llamadas al LLM aparece con ceros, y eso también es información: así
se descubre que la recuperación, la etapa que todo el mundo supone cara, no cuesta
nada por petición.

**Un tramo que falla también gasta** — a menudo *más*, porque agotó reintentos
antes de rendirse. Por eso `stage.failed` lleva el coste igual que
`stage.completed`: omitirlo escondería precisamente las peticiones más caras.

---

## Dos herramientas, dos trabajos

| | Panel autocontenido | Logfire |
|---|---|---|
| Fichero | `ai-service/eval/dashboard.py` | `app/domain/graph/observability.py` |
| Da | Latencia, coste, error, abstención, A/B, coste por tramo | El **trace en cascada**: una petición, sus tramos anidados y sus llamadas HTTP |
| Necesita | Nada | Cuenta y `LOGFIRE_TOKEN` |
| Cuándo | Siempre | Cuando quieres *ver* dónde se fue el tiempo |

Sin `LOGFIRE_TOKEN` la instrumentación se ejecuta y **no exporta nada**
(`send_to_logfire="if-token-present"`): la observabilidad nunca rompe el arranque.

```bash
# el panel, sobre los logs reales
docker compose logs --no-log-prefix ai-service | python3 ai-service/eval/dashboard.py

# y como fichero para compartir
... | python3 ai-service/eval/dashboard.py --html dashboard.html --json dashboard.json
```

### El panel dentro del producto

El HTML generado se sirve en `GET /api/v1/eval/dashboard` (`app/api/eval_reports.py`)
y el backend de negocio lo publica en **`/rag/dashboard`**, embebido en un iframe
para que conserve su propio documento y su propia paleta. Desde la Sesión 15 el
servicio IA no publica puerto alguno, así que ese es el único camino de entrada —
y por eso mismo la ruta **no** está exenta del `X-Service-Token`: latencia, coste
y tasa de error por endpoint son un perfil operativo, no una página pública.

No se genera al abrir la página: un contenedor no puede leer su propio log de
Docker. Los logs salen del host y entran por `stdin` al generador que corre dentro
del contenedor, que escribe en el volumen `eval_reports` (declarado en
`docker-compose.prod.yml`, para que el panel sobreviva a los redespliegues):

```bash
# en la instancia
cd /opt/estimator && bash scripts/refresh_dashboard.sh     # logs reales
bash scripts/refresh_dashboard.sh --sample                 # el log de muestra del repo
```

Aviso para una demo: el HTML sólo pinta las cuatro tarjetas, el sparkline y la
tabla por endpoint. El desglose **por tramo** y el **A/B** viven únicamente en la
salida de terminal (`render_terminal`), así que esos dos se enseñan en la consola.

---

## Alertas

Umbrales que tú eliges, no detección de anomalías. Un número escrito es un número
que se puede discutir en una revisión; un modelo que decide qué es "raro" es una
cosa más que puede estar silenciosamente mal y que nadie audita nunca.

```bash
docker compose logs --no-log-prefix ai-service | python3 ai-service/eval/dashboard.py \
    --alert-p95-ms 180000 \
    --alert-cost-usd 0.50 \
    --alert-error-rate 0.05
```

Sale con código distinto de cero si algo se pasa, así que sirve tal cual en un
`cron` o en un runbook sin arrastrar una pila de monitorización para responder a
tres preguntas.

**Un umbral ausente significa "no vigilado"**, no "vigilado con un valor por
defecto que nadie eligió".

### De dónde salen los umbrales

De los datos, no de la intuición. Con las cifras medidas contra la instancia:

| Umbral | Valor | Por qué |
|---|---|---|
| p95 latencia | 180 s | Es donde Rails cuelga la llamada; por encima, el usuario ya no está |
| Coste/petición | 0,50 $ | ~3× la media medida: pilla un salto, no la variación normal |
| Tasa de error | 5% | Por encima, algo estructural está roto, no es mala suerte |

---

## El bucle de realimentación

Una alerta que nadie convierte en una prueba se repetirá. Cuando una petición
falla de verdad en producción, su transcripción se convierte en caso permanente
del golden set:

```bash
python3 ai-service/eval/capture_case.py --transcript-file bad.txt \
    --id case-007-erp-integration --expected 140 --range 90 210 \
    --notes "Run de producción 2026-08-26: devolvió 640, leyó horas como jornadas"
```

A partir de ahí, ese fallo tiene que pasar el regression gate para siempre. Es
deliberadamente **manual**: exige la única decisión de la que está hecho un golden
set — *¿qué debería haber respondido?* — y un script que se inventara ese valor
estaría midiendo el sistema contra sí mismo.

---

## Punto ciego conocido

El agente de la Sesión 12 conduce la Responses API a mano en vez de pasar por
`LLMWrapper`, así que **sus tokens no llegan al acumulador**. Sus peticiones
aparecen con coste 0. Es un punto ciego conocido, no una llamada gratis, y está
escrito en el pie del propio panel para que nadie lo lea como un ahorro.
