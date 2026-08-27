"""Session 16 — the production dashboard, served instead of emailed around.

``eval/dashboard.py`` renders a self-contained HTML page from the service's own
structured logs. Until now the only way to look at it was to open the file, which
means the one artefact that says how the deployed system is behaving lives on
whichever laptop last generated it. This router hands it to the business backend
so it becomes a screen of the product like any other.

Two decisions worth naming:

* **It serves a file, it does not render one.** Generating the page on request
  would mean reading the container's own Docker logs from inside the container,
  which a container cannot do. The page is refreshed out-of-band
  (``scripts/refresh_dashboard.sh``) and this endpoint only publishes the result.
  That also keeps a page-load free of work, which matters for something people
  leave open on a second monitor.

* **It is NOT exempt from ``X-Service-Token``.** Latency, cost and error rates
  per endpoint are an operational profile of the system; the business backend
  already carries the token, so there is no reason to publish this to anyone who
  finds the URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = structlog.get_logger()

router = APIRouter(prefix="/api/v1/eval", tags=["eval"])

# ``app/api/eval_reports.py`` → ``ai-service/`` → ``eval/reports``. The Dockerfile
# copies ``eval/`` into the image, so this resolves both in the container and in
# a local ``uv run uvicorn``.
REPORTS_DIR = Path(__file__).resolve().parents[2] / "eval" / "reports"
DASHBOARD_HTML = REPORTS_DIR / "dashboard.html"
DASHBOARD_JSON = REPORTS_DIR / "dashboard.json"

# Shown when the page has never been generated. A 404 would be technically
# correct and operationally useless: the person looking at it needs the command,
# not the status code.
_MISSING = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Panel de señales — todavía no generado</title>
<style>
  body { margin:0; padding:3rem 1.5rem; background:#fbfaf9; color:#1a1a19;
         font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",sans-serif; }
  @media (prefers-color-scheme: dark) { body { background:#141413; color:#f0eee6; } }
  main { max-width:640px; margin:0 auto; }
  code { font-family:ui-monospace,monospace; font-size:.85rem; }
  pre { padding:1rem; border-radius:8px; background:rgba(127,127,127,.12); overflow-x:auto; }
</style></head>
<body><main>
  <h1>El panel todavía no se ha generado</h1>
  <p>El panel se construye a partir de los logs estructurados del servicio, no en
  caliente: un contenedor no puede leer su propio log de Docker. Para generarlo,
  en la instancia:</p>
  <pre><code>cd /opt/estimator &amp;&amp; bash scripts/refresh_dashboard.sh</code></pre>
  <p>Y recarga esta página.</p>
</main></body></html>
"""


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    summary="The rendered production-signals dashboard",
)
async def dashboard() -> HTMLResponse:
    """The self-contained HTML page, or instructions for producing it."""
    if not DASHBOARD_HTML.is_file():
        log.info("eval_dashboard_missing", path=str(DASHBOARD_HTML))
        return HTMLResponse(_MISSING, status_code=200)
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))


@router.get("/dashboard.json", summary="The dashboard aggregates, as data")
async def dashboard_json() -> dict:
    """The same numbers the page shows, for anything that wants to compute on them.

    Empty (rather than 404) when the dashboard has not been generated, so a caller
    can render "sin datos todavía" without special-casing an error.
    """
    if not DASHBOARD_JSON.is_file():
        return {"generated_at": None, "overall": {}, "by_path": [], "sparkline": []}
    return json.loads(DASHBOARD_JSON.read_text(encoding="utf-8"))
