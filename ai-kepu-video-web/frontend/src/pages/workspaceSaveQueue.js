const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

/** Versioned, durable edits. Server acknowledgements only consume their own revision. */
export class WorkspaceSaveQueue {
  constructor({ pending = {}, send, refresh, onChange = () => {}, onSaved = () => {}, auto = false }) {
    this.entries = pending.version === 2 ? structuredClone(pending.segments || {}) : Object.fromEntries(
      Object.entries(pending).map(([index, patch]) => [index, Object.fromEntries(
        Object.entries(patch).map(([field, value]) => [field, { value, revision: 0 }]))]))
    this.sequence = Math.max(0, ...Object.values(this.entries).flatMap(fields => Object.values(fields).map(e => e.revision || 0)))
    Object.assign(this, { send, refresh, onChange, onSaved, auto })
    this.server = {}
    this.version = -1
    this.stopped = false
  }
  patches() { return Object.fromEntries(Object.entries(this.entries).map(([index, fields]) => [index, Object.fromEntries(Object.entries(fields).map(([field, e]) => [field, e.value]))])) }
  serialize() { return Object.keys(this.entries).length ? { version: 2, segments: this.entries } : {} }
  conflicts() { return Object.entries(this.entries).flatMap(([index, fields]) => Object.entries(fields).filter(([, e]) => e.conflict).map(([field, e]) => ({ index, field, value: e.value, server: e.server }))) }
  notify(message) { if (!this.stopped) this.onChange(this.patches(), this.serialize(), message) }
  schedule() {
    clearTimeout(this.timer)
    if (this.auto && !this.failed && !this.stopped && Object.keys(this.entries).length && !this.conflicts().length) {
      this.timer = setTimeout(() => this.flush().catch(() => {}), 320)
    }
  }
  rebase(data) {
    if (this.inFlight || Number(data.plan_version) < this.version) return
    this.version = Number(data.plan_version) || 0
    this.server = Object.fromEntries((data.segments || []).map(s => [s.segment_index, s]))
    for (const [index, fields] of Object.entries(this.entries)) {
      for (const [field, e] of Object.entries(fields)) {
        const row = this.server[index]
        const value = row?.[field]
        if (row && same(value, e.value)) delete fields[field]
        else if (!row || (Object.hasOwn(e, 'base') && !same(value, e.base))) Object.assign(e, { conflict: true, server: value })
        else Object.assign(e, { base: value, conflict: false })
      }
      if (!Object.keys(fields).length) delete this.entries[index]
    }
    this.notify(this.conflicts().length ? '编辑冲突，内容已保留' : Object.keys(this.entries).length ? (this.failed ? '保存失败，内容已保留' : '等待保存…') : '已同步')
    this.schedule()
  }
  edit(index, patch) {
    this.failed = false
    this.entries[index] ||= {}
    for (const [field, value] of Object.entries(patch)) {
      const prior = this.entries[index][field]
      this.entries[index][field] = { ...prior, value, base: prior ? prior.base : this.server[index]?.[field], revision: ++this.sequence }
    }
    this.notify('等待保存…')
    this.schedule()
  }
  resolve(index, field, keepMine) {
    const entry = this.entries[index]?.[field]
    if (!entry) return
    if (keepMine && !this.server[index]) return
    if (keepMine) Object.assign(entry, { base: entry.server, conflict: false, revision: ++this.sequence })
    else delete this.entries[index][field]
    if (!Object.keys(this.entries[index]).length) delete this.entries[index]
    this.notify(Object.keys(this.entries).length ? '等待保存…' : '已同步')
    this.schedule()
  }
  flush() {
    this.failed = false
    clearTimeout(this.timer)
    if (this.running) return this.running
    this.running = this.drain().finally(() => { this.running = null })
    return this.running
  }
  async drain() {
    let rebases = 0
    try {
      while (!this.stopped && Object.keys(this.entries).length) {
        const next = Object.entries(this.entries).map(([index, fields]) => [index, Object.fromEntries(Object.entries(fields).filter(([, e]) => !e.conflict))]).find(([, f]) => Object.keys(f).length)
        if (!next) throw new Error('编辑冲突，请选择保留哪份内容')
        const [index, fields] = next
        const sent = structuredClone(fields)
        const patch = Object.fromEntries(Object.entries(sent).map(([field, e]) => [field, e.value]))
        this.inFlight = true
        this.notify('正在保存…')
        let result
        try { result = await this.send(Number(index), { ...patch, expected_plan_version: this.version }) }
        catch (error) {
          this.inFlight = false
          if (error?.response?.status === 409 && this.refresh && rebases++ < 2) {
            this.rebase(await this.refresh())
            continue
          }
          throw error
        }
        this.inFlight = false
        if (this.stopped) return
        this.version = result.plan_version
        this.server[index] = { ...this.server[index], ...patch }
        for (const [field, entry] of Object.entries(sent)) {
          const current = this.entries[index]?.[field]
          if (current?.revision === entry.revision) delete this.entries[index][field]
          else if (current) current.base = entry.value
        }
        if (this.entries[index] && !Object.keys(this.entries[index]).length) delete this.entries[index]
        this.onSaved(result)
        this.notify(Object.keys(this.entries).length ? '等待保存…' : '已同步')
      }
    } catch (error) {
      this.failed = true
      this.notify(this.conflicts().length ? '编辑冲突，内容已保留' : '保存失败，内容已保留')
      throw error
    } finally { this.inFlight = false }
  }
  stop() { this.stopped = true; clearTimeout(this.timer) }
}
