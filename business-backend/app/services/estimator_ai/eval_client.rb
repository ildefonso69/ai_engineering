# Session 16 — the production-signals dashboard, fetched from the AI service.
#
# The panel is generated from the AI service's own structured logs and lives on a
# volume next to that service. It is not a page this application renders: it is a
# page this application PUBLISHES, so the team can look at latency, cost and error
# rate without an SSH session.
#
# Why fetch it rather than link to it: since Session 15 the AI service publishes
# no port. The only way in is through this backend, which already carries the
# X-Service-Token — and the dashboard is deliberately not exempt from it.
module EstimatorAi
  class EvalClient < BaseClient
    # The rendered HTML page. Returns a String, not a parsed body, which is why it
    # bypasses ``handle_response``: that helper expects JSON and would try to make
    # sense of a document.
    def dashboard_html
      response = json_conn.get("/api/v1/eval/dashboard")
      raise_for_status(response)
      response.body.to_s
    end

    # The same aggregates as data, for anything that wants to compute on them
    # rather than display them.
    def dashboard_data
      handle_response(json_conn.get("/api/v1/eval/dashboard.json"))
    end

    private

    # ``json_conn`` parses JSON responses; an HTML body comes back as a String, so
    # only the status needs interpreting. Delegating to ``handle_response`` for the
    # failure cases keeps the error taxonomy identical to every other client.
    def raise_for_status(response)
      handle_response(response) unless response.status == 200
    end
  end
end
