import { Check, Copy, Pencil, Plus, Star, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router'

import {
  createProductionTemplate,
  deleteProductionTemplate,
  listProductionTemplates,
  updateProductionTemplate,
} from '../api/task'
import { ConfirmDialog, Modal } from '../components/Modal'
import { toast } from '../lib/toast'

const NEW_TEMPLATE = {
  name: '我的生产预设',
  text_style: '知识科普',
  visual_style: '电影质感',
  ratio: '16:9',
  subtitle_options: { size: 'standard', position: 'standard', outline: 'standard' },
  generation_options: { prompt_concurrency: 4, image_concurrency: 8, retry_count: 2, retry_interval_seconds: 5 },
}

export function TemplatesPage() {
  const navigate = useNavigate()
  const [templates, setTemplates] = useState([])
  const [loading, setLoading] = useState(true)
  const [editor, setEditor] = useState(null)
  const [pendingDelete, setPendingDelete] = useState(null)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const result = await listProductionTemplates()
      setTemplates(result?.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const openCreate = source => {
    setEditor({ mode: 'create', source, name: source ? `${source.name} 副本` : NEW_TEMPLATE.name })
  }

  const openRename = template => {
    setEditor({ mode: 'rename', source: template, name: template.name })
  }

  const saveEditor = async () => {
    const name = editor?.name?.trim()
    if (!name) return
    setSaving(true)
    try {
      if (editor.mode === 'rename') {
        await updateProductionTemplate(editor.source.template_id, { name })
        toast.success('模板名称已更新')
      } else {
        await createProductionTemplate({ ...(editor.source || NEW_TEMPLATE), name, is_default: false })
        toast.success('生产模板已保存')
      }
      setEditor(null)
      await load()
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!pendingDelete) return
    setSaving(true)
    try {
      await deleteProductionTemplate(pendingDelete.template_id)
      toast.success('模板已删除')
      setPendingDelete(null)
      await load()
    } finally {
      setSaving(false)
    }
  }

  const setDefault = async template => {
    await updateProductionTemplate(template.template_id, { is_default: true })
    toast.success('已设为默认生产模板')
    load()
  }

  return <main className="catalog-page template-page">
    <header className="catalog-heading">
      <div><p className="eyebrow">PRODUCTION PRESETS</p><h1>生产模板</h1><p>保存真正会影响后续生产的画面、配音、字幕与重试策略。</p></div>
      <button type="button" className="button button-primary" onClick={() => openCreate()}><Plus size={17} /> 新建模板</button>
    </header>
    {loading ? <div className="catalog-loading">正在读取本地模板…</div> : templates.length ? <section className="template-grid">
      {templates.map(template => <article className="template-card" key={template.template_id}>
        <header><span>{template.is_default ? <><Star size={14} fill="currentColor" /> 默认模板</> : '用户预设'}</span><strong>{template.name}</strong></header>
        <dl>
          <div><dt>画面</dt><dd>{template.visual_style} · {template.ratio}</dd></div>
          <div><dt>文稿</dt><dd>{template.text_style}</dd></div>
          <div><dt>字幕</dt><dd>{template.subtitle_options?.size || 'standard'} · {template.subtitle_options?.position || 'standard'}</dd></div>
          <div><dt>生成</dt><dd>生图 {template.generation_options?.image_concurrency || 8} 路 · 重试 {template.generation_options?.retry_count ?? 2} 次</dd></div>
        </dl>
        <footer>
          <button type="button" onClick={() => navigate(`/manuscript?template=${template.template_id}`)}><Check size={15} /> 用于新文稿</button>
          <button type="button" onClick={() => openRename(template)}><Pencil size={15} /> 重命名</button>
          <button type="button" onClick={() => openCreate(template)}><Copy size={15} /> 复制</button>
          {!template.is_default ? <button type="button" onClick={() => setDefault(template)}><Star size={15} /> 设为默认</button> : null}
          <button type="button" className="danger-link" onClick={() => setPendingDelete(template)}><Trash2 size={15} /> 删除</button>
        </footer>
      </article>)}
    </section> : <section className="catalog-empty"><h2>还没有生产模板</h2><p>先保存一个常用配置，下一次创建项目时就不需要重新逐项设置。</p><button type="button" className="button button-primary" onClick={() => openCreate()}><Plus size={17} /> 创建第一个模板</button></section>}
    <Modal
      open={Boolean(editor)}
      title={editor?.mode === 'rename' ? '重命名生产模板' : editor?.source ? '复制生产模板' : '新建生产模板'}
      onClose={() => !saving && setEditor(null)}
      footer={<><button type="button" className="button button-secondary" onClick={() => setEditor(null)} disabled={saving}>取消</button><button type="button" className="button button-primary" onClick={saveEditor} disabled={saving || !editor?.name?.trim()}>{saving ? '正在保存…' : '保存模板'}</button></>}
    >
      <label className="field">
        <span>模板名称</span>
        <input data-modal-initial-focus value={editor?.name || ''} maxLength="60" onChange={event => setEditor(current => ({ ...current, name: event.target.value }))} onKeyDown={event => { if (event.key === 'Enter') saveEditor() }} />
      </label>
    </Modal>
    <ConfirmDialog
      open={Boolean(pendingDelete)}
      title="删除生产模板"
      message={pendingDelete ? `删除“${pendingDelete.name}”？已经创建的项目不会被修改。` : ''}
      confirmLabel="删除模板"
      danger
      confirmDisabled={saving}
      onConfirm={remove}
      onClose={() => !saving && setPendingDelete(null)}
    />
  </main>
}
