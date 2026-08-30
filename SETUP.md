# Setup — Estimator + Rails

## Requisitos
- Docker + Docker Compose
- OpenAI API key (o Anthropic)

## Instalación

### 1. Configurar `.env` en estimator

```bash
cd estimator
cp .env.example .env
```

Edita `estimator/.env` línea 2 y agrega tu clave OpenAI:
```
OPENAI_API_KEY=sk-proj-...tu-clave-aqui...
```

### 2. Configurar `.env` en estimator-web

```bash
cd estimator-web
cp .env.example .env
```

(Sin cambios necesarios si usas localhost)

## Ejecutar

### Opción A: Desde la raíz (recomendado)

```bash
cd d:\"IA Engineering\ai-engineering-main10\ai-engineering_10"
docker compose up --build
```

### Opción B: Estimator solo

```bash
cd estimator
docker compose up --build
```

Rails necesita su propia terminal desde la raíz.

## Acceso

- **Rails (frontend):** http://localhost:3000
- **FastAPI (backend):** http://localhost:8000
- **API Docs:** http://localhost:8000/docs

## Verificar salud

```bash
curl http://localhost:8000/health
curl http://localhost:3000
```

## Troubleshooting

Si estimator falla en startup:

```bash
# Limpia todo
docker system prune -af --volumes

# Levanta pasando la clave
cd estimator
OPENAI_API_KEY="sk-proj-..." docker compose up
```
