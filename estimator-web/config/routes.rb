Rails.application.routes.draw do
  # Define your application routes per the DSL in https://guides.rubyonrails.org/routing.html

  # Reveal health status on /up that returns 200 if the app boots with no exceptions, otherwise 500.
  # Can be used by load balancers and uptime monitors to verify that the app is live.
  get "up" => "rails/health#show", as: :rails_health_check

  # Render dynamic PWA files from app/views/pwa/* (remember to link manifest in application.html.erb)
  # get "manifest" => "rails/pwa#manifest", as: :pwa_manifest
  # get "service-worker" => "rails/pwa#service_worker", as: :pwa_service_worker

  resources :estimations, only: [ :index, :new, :create, :show ]

  # Session 5 conversational flow. ``create`` is bound to a specific session
  # (POST /chat_sessions/:id) — :new creates the underlying session lazily
  # when the page first loads.
  resources :chat_sessions, only: [ :new, :show, :destroy ] do
    member do
      post :create
    end
  end

  # Session 7 RAG context: chunking strategy comparison lab.
  # Session 9 RAG context: the transcript → grounded-estimate wizard. One
  # canonical resource + a member action per pipeline stage (each re-runnable),
  # plus the human-verification PATCH.
  namespace :rag do
    resources :chunking_comparisons, only: [ :index, :new, :create, :show ]

    # Corpus / Índice (Session 11): add new information to the vector DB and poll
    # the async indexing job until the corpus grows.
    resources :index_runs, only: [ :index, :new, :create, :show ] do
      member { get :status }
    end

    resources :estimation_runs, only: [ :index, :new, :create, :show ] do
      member do
        post  :reformulate
        post  :generate       # S12 agent proposes the structure (free decomposition)
        post  :estimate_hours # save reviewed structure → deterministic hours + agent recovery
        patch :verify         # edit hours/rates, compute cost, confirm + store
      end
    end
  end

  # Session 12 — agents console: named, personalizable profiles for the
  # hand-written agent (the ACB is shown read-only here).
  namespace :agents do
    resources :profiles
  end

  # Runtime model configuration of the AI service (Ajustes).
  resource :ai_settings, only: [ :show, :update ]

  # Landing dashboard: one card per context of the Master's journey.
  root "home#index"
end
