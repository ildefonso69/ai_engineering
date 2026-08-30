# Mirror of the FastAPI ``TaskItem``: one concrete engineering task inside a
# functional module. ``sources`` are the chunk ids backing the task.
module Rag
  class TaskItemView
    include ActiveModel::Model
    include ActiveModel::Attributes

    attribute :name, :string
    attribute :description, :string
    attribute :engineer_days, :integer, default: 0

    attr_reader :sources

    def self.from_hash(hash)
      new(hash || {})
    end

    def initialize(attributes = {})
      stringified = (attributes || {}).transform_keys(&:to_s)
      @sources = Array(stringified.delete("sources")).map(&:to_i)
      super(stringified.slice("name", "description", "engineer_days"))
    end

    def sources_label = sources.join(", ")
  end
end
