# Modos de fallo — Sesión 15, Bloque 1

Cinco reproducciones mínimas de lo que se rompe al containerizar el sistema. Son
los fallos que traen los alumnos del pre-ejercicio, aislados para poder verlos y
arreglarlos en 15 minutos sin depender de que el portátil de nadie falle en
directo.

**Ninguno se ejecuta solo.** Son ficheros de fixture: compose, Dockerfile y `.env`
con el defecto dentro y el porqué explicado arriba. `tests/test_failure_modes_s15.py`
los fija —comprueba que el defecto sigue ahí y que el fichero real **no** lo
tiene—, así que no se pudren en silencio cuando alguien arregla el original.

| # | Fichero | Síntoma | Causa |
|---|---|---|---|
| 1 | `01-image-does-not-build.Dockerfile` | `"/ai-service/uv.lock": not found` | Contexto de build equivocado (la trampa del monorepo) + `COPY . .` antes de instalar |
| 2 | `02-wrong-boot-order.yml` | `PG::ConnectionBad` en el primer arranque, bien en el segundo | `depends_on` en forma de lista: espera creación, no disponibilidad |
| 3 | `03-localhost-vs-service-name.yml` | `Failed to open TCP connection to localhost:8000` | Dentro de un contenedor, `localhost` es ese contenedor |
| 4 | `04-ports-leak.yml` | Todo funciona (ese es el problema) | Un `ports:` en el servicio IA rompe la frontera |
| 5 | `05-token-mismatch.env` | 401 en todo, con el stack "healthy" | Las dos capas tienen valores distintos del secreto |

## Cómo se usan en el directo

Para cada uno: **se enseña el síntoma → se abre el fichero → se arregla en
pantalla → se compara con el fichero real del repo.**

```bash
# El único que se puede ejecutar de verdad en segundos (falla a propósito):
cd ai-service
docker build -f exercises/session-15/failure_modes/01-image-does-not-build.Dockerfile . 2>&1 | tail -3

# Los demás se leen y se contrastan con el real:
diff <(grep -A3 'ai-service:' exercises/session-15/failure_modes/04-ports-leak.yml) \
     <(grep -A3 '^  ai-service:' ../docker-compose.yml)

# Y la comprobación que los cubre todos, sin red ni Docker:
uv run pytest tests/test_failure_modes_s15.py -v
```

## El orden importa

Están ordenados por **cuándo te muerden**: 1 al construir, 2–3 al arrancar,
4–5 cuando ya "funciona". Los dos últimos son los peligrosos, porque el sistema
no se queja: el 4 deja la puerta abierta y el 5 la cierra tanto que no entra
nadie —o, en su variante silenciosa (token vacío), no la cierra en absoluto.
