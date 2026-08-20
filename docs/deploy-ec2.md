# Despliegue en una instancia EC2

Sesión 15 · Módulo 6

El sistema se despliega en **una sola máquina virtual** de AWS con Docker
Compose, un reverse proxy que termina TLS y systemd para que sobreviva a los
reinicios.

> **Lo que cambia respecto a un PaaS.** En una plataforma gestionada, la frontera
> público/privado la expresa el proveedor: marcas un servicio como privado y deja
> de tener URL. En una VM **no hay nada que la exprese por ti**. La construyes tú,
> y por eso aquí hay tres capas en vez de una.

---

## La topología

```
                    internet
                        │
                        │  :443 (y :80 solo para redirigir y validar el certificado)
                        ▼
              ┌───────────────────┐
              │      caddy        │   ← el ÚNICO servicio con `ports:`
              │   TLS + proxy     │
              └─────────┬─────────┘
                        │ red interna de Docker
   ┌────────────────────┼─────────────────────────────────┐
   │                    ▼                                  │
   │          business-backend (Rails)  ── postgres        │
   │                    │                                  │
   │                    ▼                                  │
   │            ai-service (FastAPI) ── vector-db          │
   │                    │             └─ redis             │
   └────────────────────┼─────────────────────────────────┘
                        ▼
              proveedor LLM (saliente)
```

**Nada de lo que hay dentro de la caja publica un puerto.** Se comprueba con un
comando y se explica en "Verificar la frontera", más abajo.

---

## Las tres capas de la frontera

Ninguna basta sola, y esa es exactamente la idea:

| Capa | Qué hace | Si falla ella sola |
|---|---|---|
| **Security group de AWS** | Solo entran 22, 80 y 443 | `ufw` sigue bloqueando |
| **`ufw` en la instancia** | Lo mismo, dentro de la máquina | El security group sigue bloqueando |
| **`docker-compose.prod.yml`** | Solo `caddy` declara `ports:` | Aunque el puerto estuviera abierto, no hay nada escuchando en él |

Defensa en profundidad: cada capa asume que las otras pueden estar mal
configuradas.

---

## Requisitos previos

Lo que hay que tener **antes** de empezar:

| Requisito | Valor | Por qué |
|---|---|---|
| Instancia | Ubuntu 24.04 LTS, **t3.medium** (4 GB) | Los 5 servicios + el proxy no caben cómodamente en 2 GB |
| Disco | **≥ 30 GB** de EBS | Solo las imágenes son ~7,4 GB (el servicio IA son ~4,9 GB por torch). Los 8 GB por defecto se llenan en el primer `pull` |
| IP elástica | asociada | Sin ella la IP cambia en cada parada y el DNS deja de apuntar |
| **Dominio propio** | registro `A` → IP elástica, **ya propagado** | Let's Encrypt **no emite certificados para `*.amazonaws.com`**. Sin dominio no hay HTTPS |
| Security group | entrante: 22, 80, 443 | Nada más |
| Clave SSH | en tu máquina y en GitHub Secrets | Para desplegar |
| **Arquitectura** | las imágenes deben ser **`linux/amd64`** | Una EC2 x86 no ejecuta una imagen `arm64`. Si construyes en un Mac de Apple Silicon, **deja que las construya el CI** |

> **Comprueba el DNS antes de arrancar nada.** Si el dominio no resuelve todavía,
> el desafío ACME falla y Caddy entra en reintentos con espera creciente.
> ```bash
> dig +short tu-dominio.com     # tiene que devolver la IP elástica
> ```

---

## 1. Aprovisionar la instancia

Una sola vez, y es idempotente:

```bash
ssh ubuntu@$EC2_HOST 'bash -s' < deploy/bootstrap.sh
```

Qué hace, y por qué cada cosa:

| Paso | Por qué |
|---|---|
| Docker Engine + plugin de compose | Desde el repositorio oficial: el de Ubuntu va por detrás y no trae `docker compose` v2 |
| Límite de tamaño de logs | Sin él, una máquina de larga vida con healthchecks cada 30 s **llena el disco**, y el primer síntoma es Postgres negándose a escribir |
| 2 GB de swap | 4 GB bastan para *ejecutar*, pero van justos mientras pgvector construye los índices HNSW. Convierte un OOM en un minuto lento |
| `ufw` (22/80/443) | La segunda capa de la frontera |
| Actualizaciones de seguridad automáticas | Sin reinicio automático: no quieres que se reinicie a mitad de una demo |
| `/opt/estimator` | Dónde vive la aplicación |
| Unidad de systemd | Lo que sustituye a "la plataforma te lo reinicia" |

**Cierra la sesión SSH y vuelve a entrar** después: el grupo `docker` no se
aplica a una sesión ya abierta.

---

## 2. Llevar el repositorio y los secretos

```bash
ssh ubuntu@$EC2_HOST 'git clone https://github.com/<owner>/<repo>.git /opt/estimator'
```

Los secretos **nunca** vienen de git:

```bash
scp .env.prod ubuntu@$EC2_HOST:/opt/estimator/.env
ssh ubuntu@$EC2_HOST 'chmod 600 /opt/estimator/.env'
```

Contenido mínimo de ese `.env` (plantilla en `.env.example`):

```bash
APP_DOMAIN=estimator.tu-dominio.com
IMAGE_OWNER=<tu-usuario-de-github>
IMAGE_TAG=latest

OPENAI_API_KEY=sk-...
AI_SERVICE_TOKEN=<openssl rand -hex 32>
ESTIMATE_API_KEY=<openssl rand -hex 24>
RETRIEVAL_API_KEY=<openssl rand -hex 24>
SECRET_KEY_BASE=<openssl rand -hex 64>

POSTGRES_USER=postgres
POSTGRES_PASSWORD=<algo largo y aleatorio>
POSTGRES_DB=estimator_web_production
VECTOR_DB_USER=estimator
VECTOR_DB_PASSWORD=<algo largo y aleatorio>
VECTOR_DB_NAME=estimator
```

> **`AI_SERVICE_TOKEN` tiene que ser el mismo valor** que envía el backend de
> negocio y que valida el servicio IA. Es un único fichero para los dos, así que
> no se pueden descuadrar — que es justo el error más común cuando hay dos.

---

## 2 bis. Obtener el certificado antes de desplegar (recomendado)

El flujo normal emite el certificado cuando arranca el stack completo. Pero eso **encadena dos cosas
independientes**: si el DNS no está bien, te enteras *después* de descargar 7,4 GB de imágenes.

Levantando **solo Caddy** con un fichero mínimo se valida DNS + puerto 80 + flujo ACME en dos
minutos, y el certificado queda guardado para el despliegue real. Muy recomendable antes de una
demo.

```bash
ssh ubuntu@$EC2_HOST
```

**El detalle que hay que hacer bien: el nombre del volumen.**

```bash
docker volume create estimator_caddy_data
```

Compose derivará después `${COMPOSE_PROJECT_NAME}_caddy_data`, que es **exactamente ese nombre**.
Así el certificado que emites ahora **lo reutiliza el stack completo** en vez de pedir otro — que es
justo lo que agotaría la cuota (**5 certificados por dominio y semana**).

```bash
cat > /opt/estimator/Caddyfile.bootstrap <<'EOF'
estimations.tu-dominio.com {
	respond "estimator: TLS ok" 200
}
EOF

docker run -d --name caddy-bootstrap \
  -p 80:80 -p 443:443 \
  -v estimator_caddy_data:/data \
  -v /opt/estimator/Caddyfile.bootstrap:/etc/caddy/Caddyfile:ro \
  caddy:2.8-alpine

docker logs -f caddy-bootstrap        # esperar "certificate obtained successfully"
```

Verificar desde fuera:

```bash
curl -sI https://$APP_DOMAIN | head -1        # 200
curl -sI http://$APP_DOMAIN  | head -1        # 308 -> https
echo | openssl s_client -connect $APP_DOMAIN:443 -servername $APP_DOMAIN 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

Y **parar el Caddy temporal**, que si no ocupará el 80 y el 443 cuando arranque el stack:

```bash
docker rm -f caddy-bootstrap          # el certificado sigue en estimator_caddy_data
```

> **Si la validación falla**, el siguiente intento va contra el entorno de **staging** de Let's
> Encrypt (`acme_ca https://acme-staging-v02.api.letsencrypt.org/directory` en el Caddyfile): emite
> un certificado no confiable pero **no consume cuota**. Depurado el problema, se quita esa línea.

> **Ojo al interpretar el smoke test en este punto.** Con el placeholder respondiendo 200 a todo,
> las comprobaciones de "el backend responde" y "/up en verde" **pasan en falso**. La única que
> detecta que todavía no hay aplicación es la del badge del modelo. Es el resultado correcto: TLS y
> frontera en verde, aplicación en rojo.

---

## 3. Arrancar

```bash
ssh ubuntu@$EC2_HOST
cd /opt/estimator
sudo systemctl start estimator
```

systemd ejecuta, con `COMPOSE_PROJECT_NAME=estimator` fijado:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

> **Por qué se fija `COMPOSE_PROJECT_NAME`.** Compose deriva el nombre de los
> volúmenes del nombre del proyecto, que por defecto es el del directorio. Si
> algún día ejecutas compose desde otra carpeta, `estimator_postgres_data`
> resuelve a **otro volumen, vacío** — el corpus desaparece en silencio y hay que
> volver a pagar los embeddings. Fijarlo hace que la identidad del volumen no
> dependa de dónde esté el repositorio.

La primera vez tarda: descarga ~7,4 GB y Caddy pide el certificado.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy
# ...certificate obtained successfully...
```

---

## 4. Restaurar el corpus

Una instancia nueva arranca con pgvector **vacío**. Volver a ingestarlo significa
volver a pagar todos los embeddings; el volcado los lleva ya calculados.

```bash
# En tu máquina, con el stack local levantado
./scripts/dump_corpus.sh                       # -> backups/corpus-<fecha>.dump  (~23 MB)
scp backups/corpus-*.dump ubuntu@$EC2_HOST:/tmp/corpus.dump

# En la instancia
ssh ubuntu@$EC2_HOST 'cd /opt/estimator && ./scripts/restore_corpus.sh /tmp/corpus.dump'
```

Va **directo a Postgres**, no por la API: los endpoints de ingesta exigen el
`X-Service-Token` y además volverían a embeber todo.

El volcado incluye `alembic_version`, así que la base restaurada ya sabe qué
migraciones tiene aplicadas.

---

## 5. Verificar la frontera

La comprobación que da sentido a la sesión. **Desde tu máquina:**

```bash
# El sistema responde por HTTPS
curl -I https://$APP_DOMAIN

# HTTP redirige
curl -I http://$APP_DOMAIN            # 308

# Y nada más es alcanzable — los cuatro deben fallar
for p in 8000 3000 5432 6379; do
  echo -n "puerto $p: "
  timeout 3 bash -c "</dev/tcp/$EC2_HOST/$p" 2>/dev/null && echo "ABIERTO ⚠️" || echo "cerrado ✅"
done
```

**Desde dentro de la instancia**, donde sí se alcanzan por nombre de servicio:

```bash
ssh ubuntu@$EC2_HOST
cd /opt/estimator
CO="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

$CO exec business-backend curl -s http://ai-service:8000/health
$CO exec business-backend curl -s http://ai-service:8000/health/ready

# Sin token → 401
$CO exec business-backend curl -s -o /dev/null -w '%{http_code}\n' \
    -X POST http://ai-service:8000/api/v1/estimate
```

Y el smoke test completo, que hace todo lo anterior y además una estimación real:

```bash
python scripts/smoke_test.py --base-url https://$APP_DOMAIN
```

---

## 6. Despliegue continuo

Con el despliegue manual funcionando, el pipeline se encarga. Configura en el
repositorio de GitHub:

| Tipo | Nombre | Valor |
|---|---|---|
| Secret | `EC2_HOST` | IP elástica o dominio |
| Secret | `EC2_USER` | `ubuntu` |
| Secret | `EC2_SSH_KEY` | La clave **privada** |
| Variable | `APP_DOMAIN` | `estimator.tu-dominio.com` |

A partir de ahí, cada push a `main` que pase los tests construye las imágenes,
las publica en GHCR y las despliega. El job fija `IMAGE_TAG` al SHA del commit:
**el rollback es cambiar esa etiqueta**, no reconstruir nada.

```bash
# Rollback manual, en la instancia
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=<sha-anterior>/' .env
sudo systemctl restart estimator
```

---

## Operación diaria

```bash
sudo systemctl status estimator          # ¿está arriba?
sudo systemctl restart estimator         # reiniciar todo
journalctl -u estimator -f               # logs de la unidad

cd /opt/estimator
CO="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$CO ps                                   # estado por servicio
$CO logs -f ai-service                   # logs de un servicio
$CO exec vector-db psql -U estimator -d estimator

df -h /                                  # el disco es el recurso que se agota
docker system df                         # cuánto ocupan imágenes y volúmenes
```

> ⛔ **Nunca `docker compose down -v`.** La `-v` borra los volúmenes, incluido el
> corpus vectorial. No es un reinicio, es una pérdida de datos.

Diagnóstico cuando algo falla: [`runbooks/ai-service-down.md`](runbooks/ai-service-down.md).

---

## Problemas frecuentes

| Síntoma | Causa | Arreglo |
|---|---|---|
| Caddy no consigue certificado | El DNS no apunta ahí todavía, o el 80 está cerrado | `dig +short $APP_DOMAIN`; abre el 80 en el security group |
| `too many certificates already issued` | Reinicios repetidos perdiendo `/data` | Límite de Let's Encrypt: 5 por dominio y semana. Espera, o usa un subdominio distinto para probar |
| Rails responde **403 Blocked hosts** | `RAILS_ALLOWED_HOSTS` no coincide con el dominio | Ponlo bien y reinicia. El healthcheck sigue verde y despista, porque `/up` está excluido |
| Los assets no cargan | La imagen se construyó en modo desarrollo | El job `build` tiene que pasar `RAILS_ENV=production` |
| `no space left on device` | Imágenes viejas acumuladas | `docker image prune -af`; comprueba que la EBS tiene ≥30 GB |
| `exec format error` / `platform (linux/arm64) does not match` | Imagen construida en un Mac Apple Silicon para una EC2 x86 | Construir en CI (runners amd64). Comprobar: `docker image inspect <img> --format '{{.Architecture}}'` |
| Al cross-compilar: `At least one invalid signature was encountered` | Fallo de la emulación QEMU/Rosetta en la verificación GPG de `apt` | No es del Dockerfile. Dejar de emular: construir en amd64 nativo (CI o la propia instancia) |
| Se muere durante las migraciones | Sin memoria construyendo índices HNSW | Comprueba el swap: `swapon --show` |
| El corpus está vacío tras redesplegar | El nombre de proyecto de compose cambió | `COMPOSE_PROJECT_NAME=estimator`; comprueba `docker volume ls` |

---

## Qué falta para producción de verdad

Dicho explícitamente en vez de omitido:

1. **Un solo servidor.** Sin redundancia: si la instancia cae, el sistema cae.
2. **Sin staging.** Un único entorno significa probar en producción.
3. **Copias de seguridad sin verificar.** Nadie ha probado a restaurar una.
4. **Sin alertas.** Te enteras de una caída porque alguien la ve.
5. **`docker compose up -d` tiene corte.** Para y arranca los contenedores: hay
   unos segundos sin servicio. Un despliegue sin downtime necesita dos réplicas y
   un proxy que las alterne.
6. **Escala vertical solamente.** Qué haría falta para escalar de verdad está en
   [`scalability.md`](scalability.md) — y no es trivial.
