# CI/CD — el proceso de despliegue

Sesión 15 · Módulo 6

Antes de esta sesión el repositorio **no tenía CI**. Había un `ci.yml` dentro de
`business-backend/.github/`, pero GitHub Actions solo lee
`.github/workflows/` **de la raíz del repositorio**: ese fichero nunca se
ejecutó ni una vez. Y aunque se hubiera ejecutado, solo cubría Rails.

El pipeline vive ahora en `.github/workflows/ci.yml` y cubre los dos proyectos.

---

## Los seis jobs

Cada uno responde **una** pregunta, y esa es la disciplina que hace que un pipeline se pueda
mantener: si no sabes decir qué pregunta responde un job, sobra.

| Job | Pregunta | Notas |
|---|---|---|
| `changes` | ¿Qué cambió? | `dorny/paths-filter`. En un monorepo es la diferencia entre 2 y 20 minutos |
| `ai-service` | ¿Funciona el servicio IA? | `ruff` + 601 tests, con el LLM doblado. Necesita un service container de pgvector (ver abajo) |
| `business-backend` | ¿Funciona el backend de negocio? | `brakeman`, `rubocop`, `importmap audit` y 192 tests, con WebMock |
| `contract` | ¿Siguen entendiéndose las dos capas? | El único que ninguna de las dos suites puede sustituir |
| `build` | ¿Esto se puede empaquetar y enviar? | Construye las dos imágenes y, desde `main`, las publica en GHCR |
| `deploy` | ¿Llega a la máquina? | SSH + `pull && up -d`. Tras `vars.CD_ENABLED` |

```mermaid
graph LR
    ch["changes"] --> ai["ai-service"] --> ct["contract"] --> bd["build"] --> dp["deploy"]
    ch --> bb["business-backend"] --> bd
```

**`ai-service` y `business-backend` corren en paralelo**: son proyectos independientes. `contract`
espera a los dos porque los compara.

## Regla 1 — en CI no se llama al modelo

**Ninguna prueba del pipeline habla con OpenAI ni con Anthropic.** No es por
ahorrar unos céntimos:

- **Determinismo.** El mismo commit debe dar el mismo resultado. Un modelo
  generativo no lo garantiza, así que el pipeline empezaría a fallar por razones
  que no tienen nada que ver con el cambio.
- **Velocidad.** Una llamada real son segundos; 600 tests serían horas.
- **Rate limits.** Diez pull requests a la vez agotarían la cuota y la culpa
  parecería del código.
- **Coste.** Multiplicado por cada push, de cada persona, cada día.

Cómo se sustituye el modelo en cada suite:

| Doble | Dónde | Qué reemplaza |
|---|---|---|
| `FakeLLMWrapper` | `ai-service/tests/conftest.py` | `complete_structured_chat`, con respuestas guionizadas |
| Monkeypatch de módulo | `tests/api/*` | `retrieve`, `estimate_from_transcript`, `reformulate_query` |
| `AsyncOpenAI` falso | `tests/generation/agentic/` | La Responses API del agente S12 |
| `fakeredis` | 5 módulos | Redis |
| WebMock | `business-backend/test/` | **Todo** el servicio IA |

La única clave del pipeline es `OPENAI_API_KEY: sk-test-not-a-real-key`, un dummy
sintácticamente válido. Existe porque `app/config.py` se niega a construir
`Settings` sin clave de proveedor, así que sin ella ni siquiera se pueden
recolectar los tests.

> **Evaluar la CALIDAD del modelo sí requiere llamarlo** — y por eso es un job
> distinto, con otra cadencia. Eso es la Sesión 16.

---

## Reproducir el entorno de CI en local

**Ejecutar los mismos comandos que CI no es lo mismo que ejecutarlos en el mismo entorno.** Este
pipeline falló la primera vez por eso: el workflow pasaba `APP_ENV: test`, un valor que
`app/config.py` no acepta —solo conoce `development`, `staging` y `production`—, así que
`get_settings()` reventaba **al importar** y pytest salía con código 4 sin llegar a cargar
`conftest.py`.

En local nunca falló, porque `APP_ENV` venía del `.env` con un valor válido.

Para reproducirlo de verdad hay que partir de un entorno **vacío**:

```bash
cd ai-service
env -i PATH="$PATH" HOME="$HOME" \
  OPENAI_API_KEY=sk-test-not-a-real-key \
  APP_ENV=development \
  DATABASE_URL=postgresql+psycopg://estimator:estimator@localhost:5433/estimator \
  uv run pytest -q
```

`env -i` es la clave: sin él heredas tu `.env` y las variables de tu shell, que es justo lo que
enmascara este tipo de fallo. **El bloque `env:` del workflow es parte de lo que hay que probar**,
no decoración.

---

## Regla 2 — los secretos nunca entran al repositorio ni a los logs

```mermaid
graph LR
    subgraph repo["Repositorio (público para el equipo)"]
        ex[".env.example<br/><i>nombres, sin valores</i>"]
        wf["ci.yml<br/><i>un dummy y hooks de deploy</i>"]
    end
    subgraph mgr["Gestor de secretos de la plataforma"]
        s1["OPENAI_API_KEY"]
        s2["AI_SERVICE_TOKEN"]
        s3["DATABASE_URL"]
    end
    mgr -->|"inyectados como<br/>variables de entorno<br/>en runtime"| run["Contenedor en ejecución"]
    repo -.->|"nunca"| run
```

Tres reglas concretas:

1. **Nada de secretos en la imagen.** Se inyectan en runtime. Una imagen con una
   clave dentro filtra esa clave a cualquiera que pueda descargarla.
2. **Nada de secretos en el pipeline** más allá de los deploy hooks. Las claves de
   aplicación viven en `/opt/estimator/.env` **en la instancia**, con permisos 600, y no
   pasan por GitHub.
3. **Nada de secretos en los logs.** Nunca hagas `echo` de una variable. Para
   comparar dos valores, compara sus hashes:
   `printenv AI_SERVICE_TOKEN | sha256sum`.

GitHub enmascara los valores de `secrets.*` en los logs, pero eso es una red de
seguridad, no una política: no protege de un `curl -v` que imprima cabeceras.

### La excepción que hay hoy, y cómo revertirla

El job `build` publica en GHCR con el `GITHUB_TOKEN` del workflow — acotado al repositorio y
caducado al terminar la ejecución. Es la opción correcta.

Pero la organización `LIDR-academy` no permite publicar paquetes bajo su cuenta (`denied:
permission_denied: read_package`), así que hoy se publica bajo una cuenta personal usando dos
ajustes del repositorio:

| Tipo | Nombre | Para qué |
|---|---|---|
| Variable | `GHCR_OWNER` | La cuenta bajo la que se publica |
| Secret | `GHCR_PAT` | Un PAT classic con `write:packages` |

**Es una concesión, no una mejora.** Un PAT vive hasta que caduca o alguien lo revoca; el
`GITHUB_TOKEN` no. Está anotado aquí para que sea una decisión visible y no un descuido.

**Revertirlo cuando haya permisos:** borrar el secreto `GHCR_PAT` y poner `GHCR_OWNER` a la
organización (o borrar también la variable). El workflow usa
`${{ vars.GHCR_OWNER || github.repository_owner }}` y `${{ secrets.GHCR_PAT || secrets.GITHUB_TOKEN }}`,
así que vuelve solo al camino bueno **sin editar código**.

---

## Regla 3 — reconstruir solo lo que cambió

En un monorepo esto es la diferencia entre 2 y 20 minutos. El job `changes` usa
`dorny/paths-filter` una vez y el resto de jobs son condicionales.

Una trampa concreta: **GitHub omite un job cuyo `needs` fue omitido.** El job
`contract` depende de `ai-service`, que se omite en un cambio solo-Rails —
justo cuando el contrato más necesita comprobarse. Por eso su condición empieza
con `always()`:

```yaml
if: >-
  always() && !contains(needs.*.result, 'failure') && !cancelled() &&
  (needs.changes.outputs.ai-service == 'true' || needs.changes.outputs.business-backend == 'true')
```

---

## El contract test

El job que solo necesita un repo multi-servicio. Las dos suites pueden estar
verdes mientras las capas se han separado: renombras un endpoint en el servicio
IA, sus tests siguen pasando, y el cliente Rails se lleva un 404 en producción.

`ai-service/scripts/check_contract.py` importa la app FastAPI, genera el OpenAPI
desde los mismos modelos Pydantic que sirven el tráfico, y lo compara con
`docs/contract/business-backend-consumed-routes.json` — las 30 rutas que llaman
los clientes Ruby. Sin servidor y sin red.

También comprueba que `/health` y `/health/ready` **siguen exentos** del token:
si dejaran de estarlo, ningún contenedor volvería a reportarse sano.

```bash
cd ai-service && uv run python scripts/check_contract.py
# Contract OK -- 32 checks passed (30 consumed routes).
```

---

## Por qué CI necesita un Postgres

Cinco módulos de test entran en el `lifespan` de FastAPI, que abre un pool de
conexiones para el checkpointer de LangGraph. Sin un Postgres real, cada uno se
bloquea en el timeout del pool antes de que el error se trague: los tests
**pasan igual**, solo que tardan minutos. Un service container desechable es más
barato que la espera.

No hace falta Redis: la suite lo dobla con `fakeredis`.

El efecto es medible y vale la pena conocerlo: con el compose **estricto** (que no publica el
5433) la suite completa tarda **~9 minutos** en local; con un Postgres alcanzable, **~20 segundos**.
Los mismos 601 tests, el mismo resultado.

---

## Una lección que salió al montar esto

Siete tests fallaban en local y pasaban en CI. La causa: el plugin de `deepeval`
llama a `load_dotenv()` al cargarse, lo que vuelca el `.env` del desarrollador en
`os.environ` **antes de que corra el primer test**. Con `PRIMARY_MODEL=gpt-4o` en
un `.env` local, siete tests que asertan el valor por defecto fallaban con
`'gpt-4o' == 'gpt-4o-mini'`. En CI no hay `.env`, así que allí pasaban.

Un entorno de test que depende de la máquina no es un entorno de test. El
fixture `_hermetic_environment` de `ai-service/tests/conftest.py` limpia esas
variables, y ahora local y CI dan el mismo resultado. **Es el mismo principio
de doce factores que rige el despliegue, aplicado a los tests.**

---

## CD — tras un interruptor, no tras un comentario

El job `deploy` está escrito y cableado, y su `if:` exige **tres** cosas:

```yaml
if: github.ref == 'refs/heads/main' && github.event_name == 'push' && vars.CD_ENABLED == 'true'
```

Es decir: rama `main`, evento `push` (una ejecución manual **no** despliega: un
despliegue va siempre atado a un commit que aterrizó en main, o no hay a dónde
volver) y la variable `CD_ENABLED` puesta a `true`. Activarlo es un cambio de
configuración del repositorio, no un cambio de código que vuelva a pasar por
revisión.

Lo que hay que crear antes:

| Tipo | Nombre | Valor |
|---|---|---|
| Secret | `EC2_HOST` | La IP o el hostname de la instancia |
| Secret | `EC2_USER` | `ubuntu` |
| Secret | `EC2_SSH_KEY` | El contenido **completo** del `.pem`, cabeceras incluidas |
| Variable | `APP_DOMAIN` | El dominio público (lo usa el smoke test) |
| Variable | `CD_ENABLED` | `true` |

`EC2_SSH_KEY` es la clave **privada**: con ella se entra al servidor. Va como
*secret*, nunca como *variable* — las variables se imprimen en los logs.

**Los ficheros de despliegue viajan por `scp` desde el runner** (`docker-compose*.yml`,
`deploy/`, `scripts/`), no con un `git pull` en el servidor: `/opt/estimator` no
es un clon, y convertirlo en uno significaría dejar credenciales de git en la
máquina. El código de la aplicación no viaja por ahí — ya va dentro de las
imágenes que publicó el job anterior.

Orden importante: **primero el servicio IA, después el backend de negocio**. El
segundo depende del primero.

El smoke test corre **después** del despliegue, contra el entorno desplegado,
nunca dentro de CI — es el único que sí llama al modelo real, y por eso no puede
bloquear un commit cualquiera.

---

## Ganchos para la Sesión 16

Al final de `ci.yml` hay tres jobs comentados con el marcador `TODO(S16)`:

| Job | Qué hará | Cadencia |
|---|---|---|
| `evaluate-golden-set` | Correr el golden set contra el modelo real | Nocturna / bajo demanda |
| `regression-gate` | Comparar con la línea base y fallar si la calidad cae | Tras la evaluación |
| `ab-test` | Dos versiones de prompt sobre el mismo golden set | Bajo demanda |

Van separados de los jobs de arriba **precisamente porque llaman al modelo**: son
lentos, cuestan dinero y no son deterministas. Convertirlos en un gate de cada
commit reproduciría todos los problemas que la regla 1 evita.
