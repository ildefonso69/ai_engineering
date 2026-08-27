#!/usr/bin/env bash
# =============================================================================
# Sesión 16 — regenerar el panel de señales con los logs REALES de producción
# =============================================================================
# Uso, en la instancia:
#
#     cd /opt/estimator && bash scripts/refresh_dashboard.sh
#     bash scripts/refresh_dashboard.sh --sample     # sobre el log de muestra
#
# Después, recargar https://$APP_DOMAIN/rag/dashboard.
#
# POR QUÉ ESTE RODEO. El generador (`eval/dashboard.py`) vive dentro de la imagen
# del servicio IA, pero los logs los tiene el DEMONIO de Docker del host: un
# contenedor no puede leer su propio log. Así que los logs salen del host y
# entran por STDIN al generador que corre dentro del contenedor, que escribe en
# el volumen `eval_reports` — que es de donde los sirve
# `GET /api/v1/eval/dashboard`.
#
#     docker logs (host) ──stdin──▶ dashboard.py (contenedor) ──▶ volumen ──▶ HTTP
#
# La alternativa habría sido que el servicio escribiera además sus logs a un
# fichero para poder leerse a sí mismo. Eso es un segundo camino de logging que
# mantener, con su propia rotación, para no ganar nada: el panel no necesita ser
# tiempo real, necesita ser cierto.
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

CO=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)
OUT_HTML=/app/eval/reports/dashboard.html
OUT_JSON=/app/eval/reports/dashboard.json

if [[ "${1:-}" == "--sample" ]]; then
  # Plan B para una demo: el log de muestra que viaja en el repo. Reproducible y
  # con datos suficientes para que el panel se vea; no es producción y hay que
  # decirlo en voz alta si se proyecta.
  echo "→ Generando el panel sobre el log de MUESTRA (no es producción)"
  "${CO[@]}" exec -T ai-service python /app/eval/dashboard.py \
    --log-file /app/eval/reports/sample-production.log \
    --html "$OUT_HTML" --json "$OUT_JSON"
else
  echo "→ Generando el panel sobre los logs REALES del servicio IA"
  # `--no-log-prefix`: sin él cada línea llega como "ai-service  | {...}". El
  # parser lo tolera, pero el flag deja el JSON limpio y hace el pipe legible.
  "${CO[@]}" logs --no-log-prefix ai-service \
    | "${CO[@]}" exec -T ai-service python /app/eval/dashboard.py \
        --html "$OUT_HTML" --json "$OUT_JSON"
fi

echo
echo "Listo. Recarga /rag/dashboard en el backend de negocio."
