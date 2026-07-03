# This file is auto-generated from the current state of the database. Instead
# of editing this file, please use the migrations feature of Active Record to
# incrementally modify your database, and then regenerate this schema definition.
#
# This file is the source Rails uses to define your schema when running `bin/rails
# db:schema:load`. When creating a new database, `bin/rails db:schema:load` tends to
# be faster and is potentially less error prone than running all of your
# migrations from scratch. Old migrations may fail to apply correctly if those
# migrations use external dependencies or application code.
#
# It's strongly recommended that you check this file into your version control system.

ActiveRecord::Schema[8.0].define(version: 2026_07_02_120000) do
  # These are extensions that must be enabled in order to support this database
  enable_extension "pg_catalog.plpgsql"

  create_table "chat_sessions", force: :cascade do |t|
    t.string "remote_session_id", null: false
    t.jsonb "latest_metadata", default: {}, null: false
    t.integer "turn_count", default: 0, null: false
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.jsonb "runtime_snapshot", default: {}, null: false
    t.index ["remote_session_id"], name: "index_chat_sessions_on_remote_session_id", unique: true
  end

  create_table "chunking_comparisons", force: :cascade do |t|
    t.jsonb "strategies", default: [], null: false
    t.jsonb "queries", default: [], null: false
    t.integer "top_k", default: 3, null: false
    t.string "corpus_label", default: "budgets_sample", null: false
    t.integer "corpus_count", default: 0, null: false
    t.jsonb "response_payload", default: {}, null: false
    t.integer "duration_ms"
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.index ["created_at"], name: "index_chunking_comparisons_on_created_at"
  end

  create_table "estimation_runs", force: :cascade do |t|
    t.text "transcript", null: false
    t.string "status", default: "started", null: false
    t.string "current_step", default: "transcript", null: false
    t.jsonb "reformulation", default: {}, null: false
    t.jsonb "retrieval", default: {}, null: false
    t.jsonb "augmentation", default: {}, null: false
    t.jsonb "generation", default: {}, null: false
    t.jsonb "adjusted_breakdown", default: {}, null: false
    t.string "idempotency_key"
    t.integer "duration_ms"
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.jsonb "structure", default: {}, null: false
    t.jsonb "task_hours", default: {}, null: false
    t.index ["created_at"], name: "index_estimation_runs_on_created_at"
  end

  create_table "estimations", force: :cascade do |t|
    t.text "description", null: false
    t.string "project_type", null: false
    t.string "detail_level", null: false
    t.string "output_format", null: false
    t.jsonb "response_payload", default: {}, null: false
    t.string "prompt_version"
    t.boolean "cached", default: false, null: false
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.bigint "chat_session_id"
    t.index ["chat_session_id"], name: "index_estimations_on_chat_session_id"
    t.index ["created_at"], name: "index_estimations_on_created_at"
  end

  create_table "index_runs", force: :cascade do |t|
    t.string "job_id", null: false
    t.string "chunk_type", default: "budget_component", null: false
    t.integer "submitted_count", default: 0, null: false
    t.string "status", default: "pending", null: false
    t.integer "documents_processed", default: 0, null: false
    t.jsonb "before_stats", default: {}, null: false
    t.jsonb "after_stats", default: {}, null: false
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.index ["job_id"], name: "index_index_runs_on_job_id"
  end

  add_foreign_key "estimations", "chat_sessions"
end
