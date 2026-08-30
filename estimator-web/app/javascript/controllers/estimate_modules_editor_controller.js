import { Controller } from "@hotwired/stimulus"

// Editable, two-level cost breakdown for the human-verification step (S09):
// functional MODULES, each with a table of TASKS. No nested-form gem, no build.
//
// Serialization uses explicit integer indices so Rails coalesces the nested
// structure unambiguously:
//   modules[<m>][name] / [description]
//   modules[<m>][tasks][<t>][name] / [description] / [engineer_days] / [sources]
// Indices only need to be unique (gaps are fine — the controller sorts by index
// server-side). New modules/tasks get a monotonic index; deletes leave holes.
//
// Templates carry the placeholders __MIDX__ / __TIDX__, replaced on clone.
export default class extends Controller {
  static targets = ["modulesContainer", "moduleTemplate", "taskTemplate", "total", "totalField", "empty"]

  connect() {
    // Next free module index = max rendered index + 1.
    const indices = this.moduleElements.map((el) => parseInt(el.dataset.moduleIndex, 10))
    this.nextModuleIndex = indices.length ? Math.max(...indices) + 1 : 0
    this.moduleElements.forEach((el) => this.initModuleTaskCounter(el))
    this.recompute()
  }

  get moduleElements() {
    return Array.from(this.element.querySelectorAll("[data-module]"))
  }

  initModuleTaskCounter(moduleEl) {
    const taskIndices = Array.from(moduleEl.querySelectorAll("[data-task]"))
      .map((el) => parseInt(el.dataset.taskIndex, 10))
    moduleEl.dataset.nextTaskIndex = taskIndices.length ? Math.max(...taskIndices) + 1 : 0
  }

  addModule() {
    const m = this.nextModuleIndex++
    const html = this.moduleTemplateTarget.innerHTML.replaceAll("__MIDX__", m)
    const tmp = document.createElement("div")
    tmp.innerHTML = html.trim()
    const moduleEl = tmp.firstElementChild
    moduleEl.dataset.nextTaskIndex = "0"
    this.modulesContainerTarget.appendChild(moduleEl)
    this.appendTask(moduleEl) // a fresh module starts with one empty task
    this.toggleEmpty()
    this.recompute()
  }

  addTask(event) {
    const moduleEl = event.target.closest("[data-module]")
    if (moduleEl) {
      this.appendTask(moduleEl)
      this.recompute()
    }
  }

  appendTask(moduleEl) {
    const m = moduleEl.dataset.moduleIndex
    const t = parseInt(moduleEl.dataset.nextTaskIndex, 10)
    moduleEl.dataset.nextTaskIndex = t + 1
    const html = this.taskTemplateTarget.innerHTML.replaceAll("__MIDX__", m).replaceAll("__TIDX__", t)
    const tmp = document.createElement("tbody")
    tmp.innerHTML = html.trim()
    moduleEl.querySelector("[data-tasks-container]").appendChild(tmp.firstElementChild)
  }

  deleteTask(event) {
    event.target.closest("[data-task]")?.remove()
    this.recompute()
  }

  deleteModule(event) {
    event.target.closest("[data-module]")?.remove()
    this.toggleEmpty()
    this.recompute()
  }

  recompute() {
    let grand = 0
    this.moduleElements.forEach((moduleEl) => {
      let subtotal = 0
      moduleEl.querySelectorAll("[data-estimate-days]").forEach((input) => {
        const value = parseInt(input.value, 10)
        if (Number.isFinite(value)) subtotal += value
      })
      const subtotalEl = moduleEl.querySelector("[data-module-subtotal]")
      if (subtotalEl) subtotalEl.textContent = subtotal
      grand += subtotal
    })
    if (this.hasTotalTarget) this.totalTarget.textContent = grand
    if (this.hasTotalFieldTarget) this.totalFieldTarget.value = grand
  }

  toggleEmpty() {
    if (!this.hasEmptyTarget) return
    this.emptyTarget.classList.toggle("hidden", this.moduleElements.length > 0)
  }
}
