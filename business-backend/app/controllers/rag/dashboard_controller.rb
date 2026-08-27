# Session 16 — the production-signals panel as a screen of the product.
#
# Two actions on purpose. ``show`` is the framed page (navigation, context, the
# refresh instructions); ``raw`` serves the panel's own HTML with no layout, and
# ``show`` embeds it in an iframe.
#
# The iframe is not laziness. The panel is a complete document with its own
# ``body`` styles and its own light/dark palette; splicing it into this
# application's dark layout would break both. An iframe gives it the isolation it
# was written for, and keeps the AI service's page exactly as generated — which
# matters, because the same file is what someone opens over SSH.
module Rag
  class DashboardController < ApplicationController
    def show
      @generated_at = eval_client.dashboard_data["generated_at"]
    rescue EstimatorAi::Error, Faraday::Error => e
      @error = e.message
    end

    def raw
      render html: eval_client.dashboard_html.html_safe, layout: false
    rescue EstimatorAi::Error, Faraday::Error => e
      # Rendered inside the iframe, so it has to carry its own minimal styling.
      render html: helpers.tag.p(
        "No se pudo cargar el panel: #{e.message}",
        style: "font: 14px sans-serif; padding: 2rem; color: #b91c1c;"
      ), layout: false
    end

    private

    def eval_client = EstimatorAi::EvalClient.new
  end
end
