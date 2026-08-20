# Runbook — el servicio IA no responde

> **Formato pánico.** Estás leyendo esto porque algo está caído. Ve directo al
> paso 1. La explicación de por qué funciona está al final, no aquí.

**Severidad:** alta — sin servicio IA no hay estimaciones. El resto de la
plataforma (login, histórico, navegación) **sigue funcionando**: el badge del
modelo desaparece y las estimaciones fallan con un mensaje, pero nadie ve una
pantalla en blanco.

---

## Síntoma

Cualquiera de estos:

- La UI muestra *"Una dependencia del servicio IA no está disponible"* o
  *"El servicio IA no respondió a tiempo"*.
- El badge del modelo primario ha desaparecido de la barra de navegación.
- `GET /health` del servicio IA no responde, o `GET /health/ready` devuelve 503.
- En la instancia, `docker compose ps` muestra `ai-service` reiniciándose en bucle,
  o `systemctl status estimator` aparece en `failed`.

---

## 1. Localizar la capa que falla (60 segundos)

Ejecuta los tres en orden. **El primero que falle es tu problema**; los siguientes
son consecuencia.

```bash
# Local
docker compose ps
docker compose exec business-backend curl -s -m 5 http://ai-service:8000/health
docker compose exec business-backend curl -s -m 10 http://ai-service:8000/health/ready
```

```bash
# EC2 — por SSH, desde el directorio de la aplicación
ssh ubuntu@$EC2_HOST
cd /opt/estimator
CO="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

$CO ps
$CO exec business-backend curl -s -m 5  http://ai-service:8000/health
$CO exec business-backend curl -s -m 10 http://ai-service:8000/health/ready
```

> **Antes que nada, mira el disco.** En una VM de larga vida es la causa número
> uno, y tumba primero a las bases de datos:
> ```bash
> df -h /          # si está al 100%, ve a §5
> docker system df
> ```

Cómo leer el resultado:

| Resultado | Qué significa | Ve a |
|---|---|---|
| `/health` no responde | El proceso está muerto o no arrancó | **§2** |
| `/health` 200, `/health/ready` 503 | El proceso vive; una dependencia está caída | **§3** |
| Ambos 200, pero la UI falla igual | El problema está entre las capas, no en el servicio IA | **§4** |

`/health/ready` **nombra la dependencia rota** — no hace falta adivinar:

```json
{"status":"not_ready","checks":{"vector_db":{"ok":true,"detail":"ok"},
                                "redis":{"ok":false,"detail":"ConnectionError"}}}
```

---

## 2. El proceso no arranca

```bash
docker compose logs --tail=100 ai-service          # local
# EC2: journalctl -u estimator -n 100   (la unidad entera)
```

Busca la **primera** excepción, no la última.

| En los logs | Causa | Arreglo |
|---|---|---|
| `alembic upgrade head` falla / `Can't locate revision` | La cabeza de migración no coincide con la BBDD (típico al cambiar de rama) | Re-estampa la revisión correcta; ver "Problemas conocidos" en `docs/deployment-local.md` |
| `ValidationError ... OPENAI_API_KEY` | Falta la clave del proveedor | Ponla en el gestor de secretos y **recrea** el contenedor |
| `connection refused` a `vector-db` | La BBDD no está lista todavía | Espera al healthcheck; si persiste, ve a §3 |
| `Address already in use` | Otro contenedor ocupa el puerto | `docker rm -f ai-service` y arranca de nuevo |
| Nada, el log se corta | Sin memoria (OOM) | `free -h` y `swapon --show`. La construcción de índices HNSW es el pico; el swap de `bootstrap.sh` es el colchón |
| `no space left on device` | El disco lleno de imágenes o logs | `docker image prune -af`; comprueba el límite de logs de `/etc/docker/daemon.json` |

> **Un cambio en `.env` no basta con `--reload`.** Los settings son un singleton
> cacheado (`@lru_cache`). Hay que **recrear** el contenedor:
> `docker compose up -d --force-recreate ai-service`.

---

## 3. Una dependencia está caída

Según lo que haya dicho `/health/ready`:

```bash
# BBDD vectorial
docker compose exec vector-db pg_isready -U estimator -d estimator
docker compose logs --tail=50 vector-db

# Redis
docker compose exec redis redis-cli ping        # espera PONG
docker compose logs --tail=50 redis
```

| Dependencia | Impacto real si sigue caída |
|---|---|
| `vector-db` | **Bloqueante.** Sin recuperación no hay estimaciones RAG. |
| `redis` | **Degradado, no bloqueante.** Las cachés CAG y la idempotencia dejan de funcionar: todo se recalcula, más lento y más caro, pero responde. |

Reinicio de una sola dependencia (no toca las demás):

```bash
docker compose restart vector-db     # o redis
```

---

## 4. Las capas no se entienden

El servicio IA está sano pero el backend de negocio no llega a él.

```bash
# ¿Resuelve el nombre y responde?
docker compose exec business-backend curl -sv http://ai-service:8000/health 2>&1 | tail -5

# ¿Coinciden los tokens? (compara, NO los pegues en un chat)
docker compose exec ai-service       printenv AI_SERVICE_TOKEN | sha256sum
docker compose exec business-backend printenv AI_SERVICE_TOKEN | sha256sum
```

| Síntoma | Causa | Arreglo |
|---|---|---|
| `Could not resolve host: ai-service` | El backend de negocio apunta a `localhost` | `ESTIMATOR_API_BASE_URL=http://ai-service:8000` — nombre de servicio, nunca `localhost` |
| 401 en todo | Los dos hashes no coinciden | Un único valor en el `.env` de la raíz; recrea **ambos** servicios |
| 401 solo en `/v1/*` | Falta `ESTIMATE_API_KEY` / `RETRIEVAL_API_KEY` | Son independientes del token; ponlas también |
| Timeouts en estimaciones largas | `ESTIMATOR_AI_TIMEOUT` demasiado bajo | Los flujos con gpt-5 tardan 1–2 min; 180 s es el valor por defecto |

---

## 5. Reinicio seguro

De menos a más agresivo. **Sube un escalón solo si el anterior no bastó.**

```bash
# 1. Reiniciar el proceso — conserva datos y conexiones de las demás capas
docker compose restart ai-service

# 2. Recrear el contenedor — necesario tras cambiar variables de entorno
docker compose up -d --force-recreate ai-service

# 3. Reconstruir la imagen — solo si cambió código o dependencias
docker compose build ai-service && docker compose up -d ai-service

# 4. Parar todo y volver. Los datos sobreviven: están en volúmenes con nombre
docker compose down && docker compose up -d
```

> ⛔ **NUNCA `docker compose down -v` para "arreglar" algo.** La `-v` borra los
> volúmenes, y eso incluye el corpus vectorial. Volver a ingestarlo significa
> **volver a pagar los embeddings**. No es un reinicio, es una pérdida de datos.

En la EC2, con systemd por delante:

```bash
sudo systemctl restart estimator      # equivale al paso 4 (para y levanta todo)
journalctl -u estimator -f            # y mira qué dice mientras arranca
```

Para desplegar otra versión —o volver a la anterior— no se reconstruye nada: se
cambia la etiqueta de imagen y se reinicia.

```bash
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=<sha-que-funcionaba>/' /opt/estimator/.env
sudo systemctl restart estimator
```

---

## 6. Confirmar que se arregló

```bash
python scripts/smoke_test.py --base-url http://localhost:3000
```

Y desde dentro de la red, la versión completa (gasta unos tokens, pero es la
única que prueba el camino entero):

```bash
python scripts/smoke_test.py \
  --base-url http://localhost:3000 \
  --ai-url http://ai-service:8000 \
  --service-token "$AI_SERVICE_TOKEN"
```

Verde en las tres capas = incidente cerrado.

---

## 7. Escalación

| Cuándo | A quién | Con qué |
|---|---|---|
| 15 min sin identificar la capa que falla | Responsable de la plataforma | Salida de `docker compose ps` + los 100 últimos logs del servicio IA |
| El proveedor LLM devuelve 5xx o 429 sostenido | Nadie: es upstream | Comprueba su status page; considera cambiar a `FALLBACK_MODEL` con `PUT /api/v1/config/models` |
| Pérdida de datos del corpus vectorial | Responsable del servicio IA | **No re-ingestes por tu cuenta**: cuesta dinero real |
| Sospecha de secreto filtrado | Seguridad, de inmediato | Rota `AI_SERVICE_TOKEN` y la clave del proveedor; **nunca** pegues el valor en un ticket |

---

## Por qué el diagnóstico va en ese orden

Las tres comprobaciones del §1 recorren la pila **de fuera hacia dentro**, y cada
una descarta la anterior. `/health` es liveness pura (no toca nada), así que si
responde el proceso está vivo y el problema está más abajo. `/health/ready`
comprueba las dependencias duras y **nombra** la rota. Si ambas van bien, lo que
falla es la conversación entre capas — red o credenciales — y ahí solo hay dos
sospechosos: el nombre de host y el token.

Ninguna de las dos sondas llama al LLM. Eso es deliberado: un runbook que gasta
tokens en diagnosticar una caída empeora la caída.
