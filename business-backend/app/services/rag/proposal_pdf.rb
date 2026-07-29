require "prawn"
require "prawn/table"

# Built-in AFM fonts are intentional (basic PDF, no bundled TTF); silence the m17n note.
Prawn::Fonts::AFM.hide_m17n_warning = true

module Rag
  # Session 13 — a basic, self-contained PDF of the commercial proposal for a completed
  # graph run. Pure Ruby (Prawn), no system binary. Composes: title, estimate summary
  # (totals + per-module table), reliability report, and the proposal body (the
  # LLM markdown rendered as plain paragraphs). Text is sanitised to WinAnsi because
  # Prawn's built-in fonts only cover that range (LLM output carries smart quotes,
  # em dashes, bullets and the occasional emoji that would otherwise raise).
  class ProposalPdf
    BRAND = "999900".freeze # muted gold, matches the app accent in print

    def initialize(run)
      @run = run
    end

    def render
      pdf = Prawn::Document.new(page_size: "A4", margin: 48)
      heading(pdf)
      estimate_summary(pdf)
      reliability(pdf)
      body(pdf)
      pdf.render
    end

    private

    def heading(pdf)
      pdf.text clean(@run.proposal_title.presence || "Propuesta comercial"), size: 20, style: :bold
      pdf.fill_color "888888"
      pdf.text "Estimacion por grafo de agentes  -  run ##{@run.id}", size: 9
      pdf.fill_color "000000"
      pdf.move_down 16
    end

    def estimate_summary(pdf)
      section(pdf, "Resumen de la estimacion")
      pdf.text "Total: #{@run.total_engineer_days} jornadas  ·  #{@run.total_engineer_hours.round} h",
               size: 11, style: :bold
      pdf.move_down 6
      rows = [ [ "Modulo", "Tareas", "Horas" ] ]
      @run.estimate_modules.each do |mod|
        rows << [ clean(mod.name), mod.tasks.size.to_s, "#{mod.subtotal_hours.round} h" ]
      end
      return if rows.size == 1

      pdf.table(rows, header: true, width: pdf.bounds.width, cell_style: { size: 9, padding: 5 }) do |t|
        t.row(0).font_style = :bold
        t.row(0).background_color = "EEEEEE"
        t.columns(1..2).align = :right
      end
      pdf.move_down 16
    end

    def reliability(pdf)
      report = @run.analysis_report
      return unless report.present? && report["summary"].present?

      section(pdf, "Fiabilidad")
      ratio = (report["grounded_task_ratio"].to_f * 100).round
      pdf.text "Confianza: #{report['overall_confidence']}  ·  tareas ancladas: #{ratio}%",
               size: 10, style: :bold
      pdf.move_down 4
      pdf.text clean(report["summary"]), size: 10
      Array(report["weak_points"]).first(8).each do |wp|
        pdf.text clean("  - [#{wp['severity']}] #{wp['area']}: #{wp['issue']}"), size: 8, color: "555555"
      end
      pdf.move_down 16
    end

    def body(pdf)
      return if @run.proposal.blank?

      section(pdf, "Propuesta")
      @run.proposal.to_s.split(/\n{2,}/).each do |block|
        block = block.strip
        next if block.empty?

        if block =~ /\A#+\s*(.+)/ # markdown heading
          pdf.move_down 4
          pdf.text clean(Regexp.last_match(1)), size: 12, style: :bold
        elsif block =~ /\A[-*]\s+/ # bullet list
          block.each_line do |line|
            item = line.sub(/\A[-*]\s+/, "").strip
            pdf.text clean("•  #{item}"), size: 10 unless item.empty?
          end
        else
          pdf.text clean(block.gsub(/\s*\n\s*/, " ")), size: 10, align: :justify
        end
        pdf.move_down 6
      end
    end

    def section(pdf, title)
      pdf.fill_color BRAND
      pdf.text clean(title.upcase), size: 10, style: :bold
      pdf.fill_color "000000"
      pdf.stroke_color "DDDDDD"
      pdf.stroke_horizontal_rule
      pdf.stroke_color "000000"
      pdf.move_down 8
    end

    # Prawn's built-in fonts are WinAnsi only. Map the common LLM/Spanish punctuation to
    # ASCII, then drop anything still outside WinAnsi (emoji, box chars) instead of raising.
    def clean(text)
      text.to_s
          .tr("’‘“”", "''\"\"")
          .gsub(/[—–]/, "-")
          .gsub("…", "...")
          .gsub(/[•·]/, "-")
          .encode("Windows-1252", undef: :replace, invalid: :replace, replace: "")
          .encode("UTF-8")
    end
  end
end
