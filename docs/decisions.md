# Decisiones y suposiciones

Sesión 15 · Módulo 6 — Arquitectura de producción y despliegue

Qué se detectó al inspeccionar el repositorio, qué se decidió y por qué. Las
decisiones están fechadas y son revisables: si una deja de tener sentido, se
cambia y se anota.

---

## 1. Stack detectado (no supuesto — comprobado en el repo)

| Pieza | Tecnología | Versión | Dónde se comprobó |
|---|---|---|---|
| Distribución | Monorepo, dos proyectos | — | `ai-service/`, `business-backend/` |
| Backend de negocio | Ruby on Rails | 8.0, Ruby 3.4.4 | `business-backend/Gemfile`, `.ruby-version` |
| Servicio IA | Python + FastAPI | Python 3.11, FastAPI ≥0.110 | `ai-service/pyproject.toml` |
| Gestor de dependencias (IA) | uv (Astral) | `uv.lock` presente | `ai-service/uv.lock` |
| BBDD relacional | PostgreSQL | 16-alpine | `docker-compose.yml` |
| **BBDD vectorial** | **pgvector** | `pgvector/pgvector:pg16` | `docker-compose.yml` |
| Caché / idempotencia | Redis **Stack** | `redis/redis-stack:7.4.0-v0` | `docker-compose.yml` |
| Proveedor LLM | OpenAI (primario), Anthropic (fallback) | vía LiteLLM + Instructor | `app/foundation/llm/` |
| Migraciones | Alembic | 5 revisiones | `ai-service/alembic/versions/` |
| ORM | SQLAlchemy 2.0 | async (asyncpg) + sync (psycopg) | `app/foundation/persistence/` |

**La BBDD vectorial y la relacional del servicio IA son el mismo motor.** pgvector
es una extensión de Postgres, así que los embeddings viven en una columna
`vector` junto al resto de los datos. No hace falta Qdrant ni Chroma.

**Redis tiene que ser `redis-stack`, no el Redis normal.** La caché semántica CAG
usa RediSearch para las consultas vectoriales, y la imagen `redis:7-alpine` no
trae ese módulo. Esta restricción condiciona la decisión 4.

---

## 2. Destino de despliegue: una instancia EC2 con Docker Compose

**Decisión:** una única VM de AWS (Ubuntu 24.04, `t3.medium`), con Docker Compose,
**Caddy** como reverse proxy que termina TLS, y **systemd** para que el sistema
sobreviva a los reinicios. Configuración en `docker-compose.prod.yml` y `deploy/`.

**Por qué una VM y no un PaaS.** Un PaaS *te regala* la propiedad que esta sesión
quiere enseñar: marcas un servicio como privado y deja de tener URL pública. Eso
es cómodo, pero esconde el mecanismo. En una VM **no hay nada que exprese la
frontera por ti** y hay que construirla:

| Capa | Qué aporta |
|---|---|
| Security group de AWS | Solo entran 22, 80 y 443 |
| `ufw` en la instancia | Lo mismo, por si el security group se toca |
| `docker-compose.prod.yml` | Solo `caddy` declara `ports:` |

Ninguna basta sola: cada una asume que las otras pueden estar mal configuradas.
Ese razonamiento —defensa en profundidad— es contenido de la sesión, y en un PaaS
no se ve.

Además obliga a hacerse cargo, explícitamente, de cuatro cosas que el PaaS asumía:
**aprovisionar** el servidor, **terminar TLS**, **arrancar tras un reinicio** y
**llevar los datos**. Cada una es un tramo del directo.

**Alternativa descartada: Kamal.** Estaba ya en el repositorio como scaffolding de
`rails new` (`config/deploy.yml` con la IP de ejemplo `192.168.0.1`), y resuelve
exactamente este caso: host único, registro y TLS con kamal-proxy. Se ha
**eliminado** en lugar de adoptarse, por dos razones: solo cubre Rails —el
servicio IA, pgvector y Redis tendrían que ser `accessories`— y sobre todo porque
tener dos historias de despliegue en un repositorio significa que ninguna es
fiable. Un `deploy.yml` que apunta a `192.168.0.1` es peor que no tener nada.

**Coste asumido:** un solo servidor, sin redundancia. Si la instancia cae, el
sistema cae. Está dicho en `docs/deploy-ec2.md`, no omitido.

## 3. Rails en producción: cuatro bases de datos → una

**Problema detectado.** Rails 8 genera un bloque `production:` multi-base
(`primary` + `cache` + `queue` + `cable`, es decir solid_cache / solid_queue /
solid_cable). Tal cual, exige **cuatro bases de datos**. Ningún plan gestionado
razonable te da cuatro para guardar tres tablas de background.

**Decisión.** Las cuatro entradas apuntan al **mismo** `DATABASE_URL`. Rails las
sigue tratando como conexiones separadas (pools y `migrations_paths` propios),
pero resuelven a una sola base física y las tablas `solid_*` conviven con las de
la aplicación.

**Consecuencia.** Separarlas más adelante es un cambio de configuración, no una
migración. A la escala de este proyecto, cuatro bases serían coste y operación
sin beneficio.

**Además:** `config/database.yml` usa ahora `url:` en lugar de host/usuario/
contraseña, porque una cadena de conexión es lo que entrega cualquier plataforma.

---

## 4. Redis: `redis-stack-server`, y en una VM todo es self-hosted

**Contexto.** La caché semántica CAG necesita **RediSearch** para sus consultas
vectoriales, que el Redis normal no trae. En un PaaS eso era un problema (el
Redis gestionado no lo incluye). En una VM la pregunta desaparece: **todo lo
ejecutamos nosotros**, así que basta con elegir la imagen correcta.

**Decisión.** En producción se usa `redis/redis-stack-server:7.4.0-v0` en lugar
del `redis/redis-stack` del compose local: es la misma RediSearch pero **sin
RedisInsight**, la interfaz web que el otro levanta en el 8001. Menos superficie
expuesta para una funcionalidad que en producción no se usa.

**Lo que sí hay que asumir:** las copias de seguridad y las actualizaciones de
las dos Postgres y de Redis son nuestras. Eso es el precio de la VM, y es la
diferencia real entre gestionado y auto-alojado — no la tecnología, la guardia.

## 5. La imagen de Rails es consciente del entorno (aditiva)

**Decisión.** `business-backend/Dockerfile` acepta `ARG RAILS_ENV=development`.
El flujo local no cambia; con `--build-arg RAILS_ENV=production` produce una
imagen de producción real: gemas sin los grupos development/test,
`assets:precompile` y ficheros estáticos servidos por la app.

**Por qué aditiva y no un segundo Dockerfile.** Dos ficheros divergen. Un
argumento mantiene una sola definición de la imagen, y la diferencia entre
entornos queda visible en tres condicionales.

**Cómo llega el argumento.** En un PaaS esto dependía de que la plataforma
propagara las variables de entorno como *build args*, que era una suposición. Aquí
es explícito: lo pasa el job `build` del pipeline
(`build-args: RAILS_ENV=production`). **Sin esa línea la imagen publicada sería de
desarrollo**: sin `assets:precompile` y con las gemas de test dentro. Nada más en
el pipeline lo define.

---

## 6. Liveness y readiness son endpoints distintos

**Decisión.** `/health` se queda **exactamente como estaba** (no comprueba nada) y
se añade `/health/ready`, que verifica BBDD vectorial y Redis y devuelve 503
nombrando la que falla.

**Por qué no unirlos.** `/health` lo llaman el `HEALTHCHECK` de Docker y el
`depends_on: service_healthy` cada 30 segundos: 2.880 veces al día. Una sonda que
consulta la base de datos convierte una latencia en un reinicio en cascada; una
que llamara al LLM sería una factura.

**Ninguna de las dos llama al modelo.** Es la regla que no se negocia.

Ambas exentas del `X-Service-Token`: un healthcheck de contenedor no puede
presentar credenciales, y ninguna revela nada más que el nombre de la dependencia
caída.

---

## 7. 503 frente a 500/502

**Problema detectado.** El servicio IA mapeaba "no tengo embedder" a **500** en 9
sitios y "la BBDD vectorial no responde" (`RetrievalError`) a **502**. Ambos son
*dependencia no disponible*, y el cliente no podía distinguir "reintenta" de
"ríndete".

**Decisión.**

| Situación | Antes | Ahora |
|---|---|---|
| Embedder / cliente OpenAI ausente | 500 | **503** |
| `RetrievalError` (BBDD vectorial no consultable) | 502 | **503** |
| El LLM upstream falló | 502 | 502 (sin cambios) |
| Fallo genuino del servicio | 500 | 500 (sin cambios) |

Y el cliente Rails gana `ServiceUnavailable` (503), `RateLimited` (429, con
`Retry-After`) y `Conflict` (409), que antes caían todos en
`"unexpected status N"`.

**Bug corregido de paso:** los tres wrappers `guard_*_errors` no rescataban
`EstimatorAi::Unauthorized`, así que un 401 del servicio IA producía una página
500 de Rails en vez de un flash accionable.

---

## 8. El contrato entre capas se verifica en CI

**Decisión.** `docs/contract/business-backend-consumed-routes.json` lista las 30
rutas que consumen los clientes Ruby; `ai-service/scripts/check_contract.py` las
valida contra el OpenAPI que FastAPI genera. Corre en CI.

**Por qué existe.** Las dos suites pueden estar verdes mientras las capas se han
separado. Es el único fallo que ninguna de las dos puede ver por sí sola.

---

## 9. CI: el pipeline se construye desde cero

**Estado previo detectado.** No había CI. Existía
`business-backend/.github/workflows/ci.yml`, pero GitHub Actions solo lee
`.github/workflows/` **de la raíz**: ese workflow **nunca se ejecutó**. Además
solo cubría Rails.

**Decisión.** Nuevo `.github/workflows/ci.yml` en la raíz, cubriendo ambos
proyectos, y se **elimina** el fichero muerto plegando sus jobs (brakeman,
rubocop, importmap audit, tests). `dependabot.yml` se mueve también a la raíz —
donde tampoco se estaba leyendo — y se amplía para vigilar el servicio IA, las
propias acciones y las imágenes Docker.


---

## 10. En CI no se llama al modelo

**Decisión.** Ninguna prueba del pipeline habla con un proveedor. La única clave
es `sk-test-not-a-real-key`, un dummy que existe solo porque `app/config.py` se
niega a construir `Settings` sin clave de proveedor.

**Por qué:** determinismo, velocidad, rate limits y coste — en ese orden. Evaluar
la *calidad* del modelo sí requiere llamarlo, y por eso es un job distinto con
otra cadencia (Sesión 16).

---

## 11. La suite de tests se hizo hermética

**Problema encontrado al montar el CI.** Siete tests fallaban en local y pasaban
en CI. El plugin de pytest de `deepeval` llama a `load_dotenv()` al cargarse y
vuelca el `.env` del desarrollador en `os.environ` antes del primer test. Con
`PRIMARY_MODEL=gpt-4o` en un `.env` local, siete tests que asertan el valor por
defecto fallaban con `'gpt-4o' == 'gpt-4o-mini'`.

**Decisión.** Un fixture `_hermetic_environment` de ámbito sesión en
`ai-service/tests/conftest.py` limpia esas variables y fija una clave dummy.
Local y CI dan ahora el mismo resultado.

**Por qué importa más de lo que parece.** Es el mismo principio de doce factores
que rige el despliegue, aplicado a los tests: si el resultado depende de la
máquina, no es una prueba.

---

## 12. Higiene: `data/evals/` fuera de la imagen

**Problema detectado.** `ai-service/data/evals/` ocupa **23 MB** (una caché de
embeddings de 756 entradas) sin trackear, y `ai-service/Dockerfile` hace
`COPY data/ /app/data/`. Se habrían horneado en la imagen en la siguiente
construcción.

**Decisión.** Excluido en `ai-service/.dockerignore` y en el `.gitignore` de la
raíz. Son artefactos de ejecución local, no fuente.

---

## 13. Self-hosting del modelo (opcional, documentado sin implementar)

El servicio IA llama al LLM a través de `LLMWrapper` → LiteLLM + Instructor, que
ya habla el protocolo de OpenAI. Cambiar a un modelo local (Ollama) o a una
instancia con GPU (vLLM / TGI) es, en la práctica, **cambiar `base_url` y el
nombre del modelo** — ambos servidores exponen una API compatible con OpenAI.

No se implementa aquí porque el proyecto no lo pide. Lo que sí se mantiene es la
propiedad que lo haría barato: **ningún módulo llama al SDK del proveedor
directamente**, salvo la excepción deliberada y documentada del agente de la S12,
que usa la Responses API a mano porque ver el bucle es el objetivo del ejercicio.

---

## 14. Se elimina Kamal

`config/deploy.yml`, `.kamal/`, `bin/kamal`, `bin/thrust` y las gemas `kamal` y
`thruster` eran scaffolding intacto de `rails new`. Se han borrado: ver §2.

Nota sobre Thruster, que se va con ellos: aporta compresión, caché HTTP y
X-Sendfile delante de Puma, y **nunca estuvo conectado** (el `CMD` del Dockerfile
no lo usaba). Hoy ese papel lo hace Caddy. Si algún día se quiere acelerar la
entrega de estáticos dentro del contenedor, volver a añadirlo es una línea — pero
que estuviera en el `Gemfile` sin usar solo generaba confusión.

---

## Suposiciones pendientes de confirmar

Se listan en vez de omitirse:

1. **El despliegue no se ha ejecutado todavía.** `docker-compose.prod.yml`,
   `deploy/bootstrap.sh` y el job `deploy` están escritos y validados
   sintácticamente (`actionlint`, `docker compose config`, `bash -n`), y la
   comprobación de que **solo `caddy` publica puertos** está verificada sobre la
   configuración resuelta. Pero nada de esto ha corrido en una EC2 real.
2. **El certificado depende del DNS.** Let's Encrypt no emite para
   `*.amazonaws.com`, así que el dominio propio no es una preferencia sino un
   requisito, y el registro `A` tiene que estar propagado **antes** del primer
   arranque.
3. **Los tiempos de Caddy** (`read_timeout 600s`) están puestos para que el proxy
   sea más paciente que la aplicación. Conviene confirmarlos contra una
   estimación larga real.
4. **La restauración del corpus** se ha probado en la dirección del volcado
   (23 MB, con `alembic_version` y las tres tablas de chunks). El `pg_restore`
   contra una instancia limpia no se ha ejecutado.

---

# Sesión 16 — LLMOps

## 15. La evaluación es un pipeline aparte, no un job más

`ci.yml` reservaba un `TODO(S16)` para "las etapas de LLMOps". Al llenarlo, la
respuesta resultó ser **otro fichero**: `.github/workflows/eval.yml`.

|  | `ci.yml` | `eval.yml` |
|---|---|---|
| Cuándo | cada commit | nocturno + bajo demanda |
| Modelo | doblado, siempre | real, siempre |
| Coste | cero | un par de dólares por ejecución |
| Rojo significa | "has roto el código" | "el sistema ha empeorado" |

Meter el segundo dentro del primero es justo el error que esta separación evita:
cada commit esperaría minutos, costaría dinero y fallaría de forma no
determinista — y en una semana alguien pondría `continue-on-error` y nadie
volvería a mirarlo.

**Lo que la S16 añadió a `ci.yml` es nada**, y eso es la señal de que la costura
que dejó la S15 estaba bien puesta.

## 16. El arnés se ejecuta DENTRO del perímetro

Desde la S15 el servicio IA no publica puertos. `eval.yml` no puede apuntar el
arnés al servicio desde un runner de GitHub, así que lo **transporta** a la
instancia y lo ejecuta allí. No es un rodeo: es la frontera haciendo su trabajo.

Dos consecuencias que merecen quedar escritas:

- **El golden set viaja desde el commit**, no desde la imagen desplegada. La vara
  de medir pertenece al código que se evalúa; añadir un caso no puede exigir un
  redespliegue antes de poder medir nada.
- **CI no guarda ninguna clave de modelo.** La evaluación gasta las credenciales
  del propio sistema desplegado, dentro de él.

## 17. Los guardrails no están donde el enunciado los pedía

El enunciado sitúa los guardrails en `ai-service/app/guardrails/` y la
observabilidad en `ai-service/app/observability.py`. En este repo van a
`app/foundation/guardrails/` y `app/foundation/observability/`, que es lo que
manda `ai-service/ARCHITECTURE.md`.

El motivo no es estético: la aritmética de límites la necesitan **dos capas que no
pueden importarse entre sí** (`generation/rag` y `domain/graph`). En `foundation`
la ven las dos, y por eso habla solo en números — ni `Estimate` ni
`RetrievedChunk` — y cada capa adapta sus propios tipos.

## 18. El guardrail de salida marca, no rechaza

Cuando una cifra no se sostiene frente a la evidencia recuperada, la estimación
**se devuelve igualmente**, marcada con `requires_human_review` y el motivo en
claro. No se descarta.

Descartar trabajo que el cliente ya ha pagado porque un umbral se movió convierte
un problema de calidad en una caída — y el umbral es justo la parte que más
probablemente esté mal al principio. La plataforma Rails **enruta** sobre esa
marca; no la vuelve a decidir. Si la recalculara habría dos respuestas para una
pregunta, y la que quedaría en el log de auditoría sería la otra.

## 19. El PII se rechaza en una ruta y se reporta en la otra

`check_input` sigue rechazando datos personales en `/api/v1/estimate`, donde la
entrada es una descripción corta de proyecto que no tiene por qué llevar un IBAN.

En `/v1/estimate/from-transcript` **se reporta**, y pasa a ser un motivo de
revisión. Una transcripción real de reunión contiene un teléfono porque alguien lo
dijo en voz alta; rechazarlas haría que el endpoint principal se negara a hacer
trabajo ordinario. La política correcta depende de la entrada, no del guardrail.

## 20. La variante B es solo el experimento de coste

B = modelo pequeño para generar + caché de embeddings. **Nada más.**

Era tentador meter también el arreglo del prompt que destapó el pre-ejercicio —
saldría una variante más barata *y* más precisa— y es exactamente lo que vuelve
inservible un resultado: cuando los números se mueven no puedes decir cuál de los
dos cambios los movió. Una variable por experimento; el arreglo del prompt tiene
su propia comparación.

El reparto **hashea el `request_id`** en vez de sortear: un reintento se queda en
su brazo, y el brazo de cualquier petición pasada se puede recalcular desde su
traza sin haber guardado nada. El porcentaje se mueve en caliente
(`PUT /api/v1/config/ab`) porque un porcentaje que exige redespliegue no es un
experimento, son dos despliegues.

## 21. Umbrales de alerta, no detección de anomalías

Un número que elegiste y escribiste se puede discutir en una revisión. Un modelo
que decide qué es "raro" es una cosa más que puede estar silenciosamente mal, y
nadie la audita nunca. Los tres umbrales salen de datos medidos, no de intuición
(ver [observabilidad](observability.md)).
