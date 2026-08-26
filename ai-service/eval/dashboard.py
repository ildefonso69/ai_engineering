#!/usr/bin/env python3
"""Session 16 — the production monitor: latency, cost and error rate from the logs.

The golden set measures quality WHEN YOU ASK. This measures what production does
ALL THE TIME. They are complementary: one is the lab test, the other is the vital
signs monitor.

It reads the structured JSON the AI service already writes to stdout and keeps
only ``request_completed`` — the single per-request event emitted by the
middleware in ``app/main.py``, which carries latency, token counts, derived cost
and status. Everything below is arithmetic on that one event; there is no agent,
no collector and no external service.

    # from the deployed instance
    ssh … "cd /opt/estimator && docker compose … logs --no-log-prefix ai-service" > ai.log
    python3 eval/dashboard.py --log-file ai.log --html eval/reports/dashboard.html

    # or straight off a pipe
    docker compose logs --no-log-prefix ai-service | python3 eval/dashboard.py

TWO THINGS IT DOES NOT SHOW, both on purpose:

* The health probes. ``/health`` is called every 30 seconds; counted, it would be
  most of the rows and "the p95 latency of the service" would really be the p95
  of a liveness probe. The middleware never emits the event for them.
* The Session 12 agent's calls. That loop drives the raw Responses API by hand
  rather than going through ``LLMWrapper``, so its tokens never reach the
  accumulator. Its requests appear with a cost of 0, which is a known blind spot
  rather than a free lunch.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

EVENT = "request_completed"
STAGE_EVENT = "stage.completed"

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def parse_events(lines: Iterable[str], event: str = EVENT) -> list[dict[str, Any]]:
    """Pull the ``request_completed`` records out of a log stream.

    Tolerant by design: a log file is a mixed bag — uvicorn's own access lines,
    Alembic output, a stray traceback, and (in development) structlog's
    human-readable console renderer instead of JSON. Anything that is not a JSON
    object carrying our event is skipped rather than fatal.
    """
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line or event not in line:
            continue
        # `docker compose logs` without --no-log-prefix writes "service  | {...}".
        if "|" in line and not line.startswith("{"):
            line = line.split("|", 1)[1].strip()
        brace = line.find("{")
        if brace == -1:
            continue
        try:
            record = json.loads(line[brace:])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("event") == event:
            events.append(record)
    return events


# --------------------------------------------------------------------------- #
# Aggregating
# --------------------------------------------------------------------------- #


def percentile(values: list[float], pct: int) -> float | None:
    """Nearest-rank percentile, so a single sample is still a valid p95."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, min(len(ordered), int(round(pct / 100 * len(ordered) + 0.5))))
    return ordered[rank - 1]


def _row(events: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(e.get("latency_ms", 0) or 0) for e in events]
    costs = [float(e.get("cost_usd", 0) or 0) for e in events]
    # 0 is the status the middleware never sets; anything >= 400 is an error, and
    # so is a request that died before producing a status.
    failures = [e for e in events if int(e.get("status", 0) or 0) >= 400 or not e.get("status")]
    return {
        "requests": len(events),
        "errors": len(failures),
        "error_rate": len(failures) / len(events) if events else 0.0,
        "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p95_ms": percentile(latencies, 95) or 0.0,
        "cost_mean_usd": sum(costs) / len(costs) if costs else 0.0,
        "cost_total_usd": sum(costs),
        "tokens_total": sum(int(e.get("total_tokens", 0) or 0) for e in events),
        "llm_calls": sum(int(e.get("llm_calls", 0) or 0) for e in events),
        # The share of requests where the system declined to answer. Read together
        # with the error rate: a rise here is the system being careful, a rise
        # there is the system being broken, and confusing the two costs you either
        # a needless incident or a missed one.
        "abstention_rate": (
            len([e for e in events if e.get("abstained")]) / len(events) if events else 0.0
        ),
    }


def aggregate(
    events: list[dict[str, Any]], stages: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_path[str(event.get("path", "?"))].append(event)

    # Only requests the split actually assigned. A forced request (X-Variant on
    # the demo, or a debugging call) is not evidence about the population, and
    # letting it into the comparison is how a demo ends up deciding a rollout.
    by_variant: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("variant") and not event.get("variant_forced"):
            by_variant[str(event["variant"])].append(event)

    paths = {path: _row(rows) for path, rows in by_path.items()}
    return {
        "by_variant": {v: _row(rows) for v, rows in sorted(by_variant.items())},
        "by_stage": _stage_rows(stages or []),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": _row(events),
        # Busiest first: the row that dominates the overall number is the one
        # worth reading first.
        "by_path": dict(sorted(paths.items(), key=lambda kv: -kv[1]["requests"])),
        "sparkline": [float(e.get("latency_ms", 0) or 0) for e in events][-60:],
    }


# --------------------------------------------------------------------------- #
# Rendering — terminal
# --------------------------------------------------------------------------- #


def _stage_rows(stages: list[dict[str, Any]]) -> dict[str, Any]:
    """Where the time and the money actually go, per pipeline stage.

    This is the table that ends arguments. "Generation is 94% of the cost and
    retrieval is 0%" settles whether a cheaper embedding model is worth anything
    in about four seconds, and it is the same table that shows a cache working.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for stage in stages:
        grouped[str(stage.get("stage", "?"))].append(stage)

    rows = {}
    for name, entries in grouped.items():
        durations = [float(e.get("duration_ms", 0) or 0) for e in entries]
        costs = [float(e.get("stage_cost_usd", 0) or 0) for e in entries]
        rows[name] = {
            "runs": len(entries),
            "duration_mean_ms": sum(durations) / len(durations) if durations else 0.0,
            "cost_total_usd": sum(costs),
            "cost_mean_usd": sum(costs) / len(costs) if costs else 0.0,
        }
    return dict(sorted(rows.items(), key=lambda kv: -kv[1]["cost_total_usd"]))


def check_alerts(
    report: dict[str, Any],
    *,
    p95_ms: float | None,
    cost_usd: float | None,
    error_rate: float | None,
) -> list[str]:
    """Threshold alerts. Returns the breaches, empty when all is well.

    Thresholds, not anomaly detection, on purpose. A number you chose and wrote
    down is a number you can argue about in a review; a model that decides what
    "unusual" means is one more thing that can be quietly wrong, and nobody ever
    audits it.
    """
    overall = report["overall"]
    breaches: list[str] = []
    if p95_ms is not None and overall["latency_p95_ms"] > p95_ms:
        breaches.append(
            f"p95 latency {overall['latency_p95_ms'] / 1000:.1f}s exceeds "
            f"{p95_ms / 1000:.1f}s"
        )
    if cost_usd is not None and overall["cost_mean_usd"] > cost_usd:
        breaches.append(
            f"cost per request ${overall['cost_mean_usd']:.4f} exceeds ${cost_usd:.4f}"
        )
    if error_rate is not None and overall["error_rate"] > error_rate:
        breaches.append(
            f"error rate {overall['error_rate'] * 100:.1f}% exceeds {error_rate * 100:.1f}%"
        )
    return breaches


def render_terminal(report: dict[str, Any]) -> str:
    overall = report["overall"]
    if not overall["requests"]:
        return (
            f"{YELLOW}No `{EVENT}` events found.{RESET}\n"
            f"{DIM}The service emits them only in production (structlog renders JSON there,\n"
            f"a human-readable console format in development), and never for the health\n"
            f"probes. Check APP_ENV and that some real traffic has happened.{RESET}"
        )

    lines = [f"AI service — production signals  {DIM}({report['generated_at']}){RESET}", ""]
    err_colour = GREEN if overall["error_rate"] == 0 else RED
    lines += [
        f"  requests          {overall['requests']}",
        f"  error rate        {err_colour}{overall['error_rate'] * 100:.1f}%{RESET} "
        f"{DIM}({overall['errors']} non-2xx){RESET}",
        f"  latency mean      {overall['latency_mean_ms'] / 1000:.2f}s",
        f"  latency p95       {overall['latency_p95_ms'] / 1000:.2f}s",
        f"  abstention rate   {overall['abstention_rate'] * 100:.1f}%",
        f"  cost / request    ${overall['cost_mean_usd']:.4f}",
        f"  cost total        ${overall['cost_total_usd']:.4f} "
        f"{DIM}({overall['tokens_total']:,} tokens over {overall['llm_calls']} LLM calls){RESET}",
        "",
        f"{DIM}  {'path':44s} {'reqs':>5s} {'err':>6s} {'mean':>8s} {'p95':>8s} {'$/req':>9s}{RESET}",
    ]
    for path, row in report["by_path"].items():
        lines.append(
            f"  {path[:44]:44s} {row['requests']:5d} {row['error_rate'] * 100:5.0f}% "
            f"{row['latency_mean_ms'] / 1000:7.2f}s {row['latency_p95_ms'] / 1000:7.2f}s "
            f"${row['cost_mean_usd']:8.4f}"
        )

    if report.get("by_variant"):
        lines += ["", f"{DIM}  A/B (assigned traffic only){RESET}",
                  f"{DIM}  {'variant':10s} {'reqs':>5s} {'p95':>8s} {'$/req':>9s} "
                  f"{'abstain':>8s}{RESET}"]
        for variant, row in report["by_variant"].items():
            lines.append(
                f"  {variant:10s} {row['requests']:5d} {row['latency_p95_ms'] / 1000:7.2f}s "
                f"${row['cost_mean_usd']:8.4f} {row['abstention_rate'] * 100:7.0f}%"
            )

    if report.get("by_stage"):
        lines += ["", f"{DIM}  where the money goes{RESET}",
                  f"{DIM}  {'stage':22s} {'runs':>5s} {'mean':>8s} {'$ total':>9s}{RESET}"]
        for stage, row in report["by_stage"].items():
            lines.append(
                f"  {stage:22s} {row['runs']:5d} {row['duration_mean_ms'] / 1000:7.2f}s "
                f"${row['cost_total_usd']:8.4f}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Rendering — self-contained HTML
# --------------------------------------------------------------------------- #


def _sparkline_svg(values: list[float]) -> str:
    if not values:
        return ""
    width, height = 640, 90
    peak = max(values) or 1.0
    step = width / max(1, len(values))
    bars = "".join(
        f'<rect x="{i * step:.1f}" y="{height - (v / peak) * height:.1f}" '
        f'width="{max(1.0, step - 1.5):.1f}" height="{(v / peak) * height:.1f}" rx="1"/>'
        for i, v in enumerate(values)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'class="spark" role="img" aria-label="Latency of the last {len(values)} requests">'
        f"{bars}</svg>"
    )


def render_html(report: dict[str, Any]) -> str:
    o = report["overall"]
    cards = [
        ("Requests", f"{o['requests']}", ""),
        ("Error rate", f"{o['error_rate'] * 100:.1f}%", f"{o['errors']} non-2xx"),
        ("Latency p95", f"{o['latency_p95_ms'] / 1000:.2f}s", f"mean {o['latency_mean_ms'] / 1000:.2f}s"),
        ("Cost / request", f"${o['cost_mean_usd']:.4f}", f"${o['cost_total_usd']:.4f} total"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(value)}</div>'
        f'<div class="hint">{html.escape(hint)}</div></div>'
        for label, value, hint in cards
    )
    rows = "".join(
        f"<tr><td class='path'>{html.escape(path)}</td>"
        f"<td>{r['requests']}</td>"
        f"<td class='{'bad' if r['error_rate'] else 'ok'}'>{r['error_rate'] * 100:.0f}%</td>"
        f"<td>{r['latency_mean_ms'] / 1000:.2f}s</td>"
        f"<td>{r['latency_p95_ms'] / 1000:.2f}s</td>"
        f"<td>${r['cost_mean_usd']:.4f}</td>"
        f"<td>{r['tokens_total']:,}</td></tr>"
        for path, r in report["by_path"].items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI service — production signals</title>
<style>
  :root {{ --bg:#fbfaf9; --fg:#1a1a19; --muted:#6b6b68; --line:#e5e3e0;
           --card:#fff; --accent:#b45309; --bad:#b91c1c; --ok:#15803d; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#141413; --fg:#f0eee6; --muted:#9a9a95; --line:#2b2b28;
             --card:#1c1c1a; --accent:#d97706; --bad:#f87171; --ok:#4ade80; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2.5rem 1.5rem; background:var(--bg); color:var(--fg);
         font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",sans-serif; }}
  main {{ max-width:900px; margin:0 auto; }}
  h1 {{ font-size:1.4rem; margin:0 0 .25rem; letter-spacing:-.01em; }}
  .meta {{ color:var(--muted); font-size:.85rem; margin-bottom:2rem; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
            gap:.85rem; margin-bottom:2rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem; }}
  .label {{ color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.06em; }}
  .value {{ font-size:1.75rem; font-weight:600; margin:.3rem 0 .1rem;
            font-variant-numeric:tabular-nums; }}
  .hint {{ color:var(--muted); font-size:.78rem; }}
  .spark {{ width:100%; height:90px; fill:var(--accent); opacity:.85;
            background:var(--card); border:1px solid var(--line); border-radius:10px; }}
  h2 {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.06em;
        color:var(--muted); margin:2rem 0 .6rem; }}
  .scroll {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; font-size:.88rem; }}
  th,td {{ text-align:right; padding:.5rem .7rem; border-bottom:1px solid var(--line);
           font-variant-numeric:tabular-nums; white-space:nowrap; }}
  th {{ color:var(--muted); font-weight:500; font-size:.75rem; text-transform:uppercase; }}
  th:first-child, td.path {{ text-align:left; font-family:ui-monospace,monospace; }}
  .bad {{ color:var(--bad); }} .ok {{ color:var(--ok); }}
  footer {{ color:var(--muted); font-size:.78rem; margin-top:2.5rem;
            border-top:1px solid var(--line); padding-top:1rem; }}
</style></head>
<body><main>
  <h1>AI service — production signals</h1>
  <div class="meta">Generated {html.escape(report["generated_at"])} · from <code>{EVENT}</code>
  events in the structured logs</div>
  <div class="cards">{card_html}</div>
  <h2>Latency, last {len(report["sparkline"])} requests</h2>
  {_sparkline_svg(report["sparkline"])}
  <h2>By endpoint</h2>
  <div class="scroll"><table>
    <thead><tr><th>path</th><th>reqs</th><th>errors</th><th>mean</th><th>p95</th>
    <th>$/req</th><th>tokens</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <footer>Health probes are excluded: called every 30s, they would dominate every
  number here. The Session 12 agent path bypasses <code>LLMWrapper</code> and so
  reports no tokens — a known blind spot, not a free request.</footer>
</main></body></html>"""


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--log-file", default=None, help="Log file (default: stdin)")
    parser.add_argument("--html", default=None, help="Also write a self-contained HTML dashboard")
    parser.add_argument("--json", default=None, help="Also write the aggregates as JSON")
    # Alert thresholds. Absent = that signal is not watched. A breach exits 1 so
    # this doubles as an operational check in cron or a runbook, without dragging
    # in a monitoring stack to answer three questions.
    parser.add_argument("--alert-p95-ms", type=float, default=None)
    parser.add_argument("--alert-cost-usd", type=float, default=None)
    parser.add_argument("--alert-error-rate", type=float, default=None,
                        help="Fraction, e.g. 0.05 for 5%%")
    args = parser.parse_args(argv)

    if args.log_file:
        lines = Path(args.log_file).read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    report = aggregate(parse_events(lines), parse_events(lines, STAGE_EVENT))
    print(render_terminal(report))

    if args.html:
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_html(report), encoding="utf-8")
        print(f"\n{DIM}dashboard → {out}{RESET}")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"{DIM}aggregates → {out}{RESET}")

    breaches = check_alerts(
        report,
        p95_ms=args.alert_p95_ms,
        cost_usd=args.alert_cost_usd,
        error_rate=args.alert_error_rate,
    )
    if breaches:
        print(f"\n{RED}ALERT{RESET}")
        for breach in breaches:
            print(f"  {RED}·{RESET} {breach}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
