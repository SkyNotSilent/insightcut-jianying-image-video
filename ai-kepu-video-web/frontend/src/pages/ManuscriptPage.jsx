import { useEffect, useMemo, useRef, useState } from 'react'
import { CheckCircle2, ClipboardPaste, FileUp, Sparkles } from 'lucide-react'
import { useNavigate, useParams, useSearchParams } from 'react-router'
import { createTask, extractDocumentText, getConfigReadiness, listProductionTemplates } from '../api/task'
import { Modal } from '../components/Modal'
import { EmptyStateCard } from '../components/ui/EmptyStateCard'
import { VisualStyleCard } from '../components/ui/VisualStyleCard'
import { toast } from '../lib/toast'
import { createDraft, estimateDuration, estimateSegments, getDraft, getLatestDraft, ratioOptions, saveDraft, textStyles, visualStyles } from '../utils/projectDrafts'
import './creation-flow.css'

const exampleScript = `1  大脑如何影响我们的决策？\n\n你是否有过这样的经历：明明知道不应该买，却在情绪低落时下单了很多东西？或者明明想要好好休息，却因为一时愤怒做了后悔的决定？\n\n这并不是你不够理智，而是情绪正在悄悄影响着你的大脑。\n\n2  情绪与大脑的关系\n\n研究表明，情绪会影响我们大脑中负责决策的区域，改变我们对风险和收益的判断。\n\n例如，在压力状态下，我们的大脑更倾向于选择即时缓解的方案，而忽略了长期后果。\n\n3  如何做出更好的决策？\n\n觉察情绪，暂停片刻，理性评估，再从过去的决策中复盘学习。`

const rotatorItems = [
  { type: 'text', text: '文稿变成视频', visualClass: 'opening-text-bloom', motionClass: 'opening-anim-bloom' },
  { type: 'text', text: '导入剪映草稿', visualClass: 'opening-text-push', motionClass: 'opening-anim-push' },
  { type: 'text', text: '素材可以逐段修改', visualClass: 'opening-text-breathe', motionClass: 'opening-anim-breathe' },
  { type: 'slots' },
  { type: 'finale' },
]
const rotatorDurations = [3200, 3000, 3500, 3200, 3400]

function createInitialDraft(draftId) {
  if (draftId) return getDraft(draftId) || createDraft({ visual_style: '吉卜力', text_style: '知识科普' })
  const latest = getLatestDraft()
  return latest && !latest.created_task_id
    ? latest
    : createDraft({ visual_style: '吉卜力', text_style: '知识科普' })
}

function normalizeTitle(value) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, 32)
}

function normalizeLength(value) {
  const number = Number(value)
  if (!Number.isFinite(number) || number <= 0) return 0
  return Math.max(50, Math.min(2000, Math.round(number / 50) * 50))
}

function readLastTtsOptions() {
  try {
    const parsed = JSON.parse(localStorage.getItem('insightcut:last-tts-options') || '{}')
    return parsed && typeof parsed === 'object' ? parsed : undefined
  } catch {
    return undefined
  }
}

export function ManuscriptPage() {
  const { draftId } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [draft, setDraft] = useState(() => createInitialDraft(draftId))
  const draftRef = useRef(draft)
  const saveTimer = useRef(null)
  const editorRef = useRef(null)
  const documentInput = useRef(null)
  const [paperFocused, setPaperFocused] = useState(false)
  const [saveState, setSaveState] = useState('saved')
  const [savedAt, setSavedAt] = useState(() => new Date())
  const [rotatorIndex, setRotatorIndex] = useState(0)
  const [starting, setStarting] = useState(false)
  const [readinessPrompt, setReadinessPrompt] = useState(null)

  useEffect(() => {
    if (!draftId) {
      navigate(`/manuscript/${draftRef.current.draft_id}`, { replace: true })
      return
    }
    const loaded = getDraft(draftId)
    if (!loaded) {
      navigate(`/manuscript/${draftRef.current.draft_id}`, { replace: true })
      return
    }
    draftRef.current = loaded
    setDraft(loaded)
  }, [draftId, navigate])

  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return undefined
    const timer = window.setTimeout(() => setRotatorIndex(index => (index + 1) % rotatorItems.length), rotatorDurations[rotatorIndex])
    return () => window.clearTimeout(timer)
  }, [rotatorIndex])

  useEffect(() => () => {
    window.clearTimeout(saveTimer.current)
    persistDraft(draftRef.current, setDraft, setSaveState, setSavedAt)
  }, [])

  const text = draft.input_mode === 'theme' ? draft.theme : draft.manuscript
  const contentLength = String(text || '').replace(/\s+/g, '').length
  const isTheme = draft.input_mode === 'theme'
  const targetLength = normalizeLength(draft.length) || 300
  const isEmpty = !text.trim() && !paperFocused

  const scheduleSave = nextDraft => {
    setSaveState('saving')
    window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => persistDraft(nextDraft || draftRef.current, setDraft, setSaveState, setSavedAt), 320)
  }

  const patchDraft = patch => {
    const next = { ...draftRef.current, ...patch }
    draftRef.current = next
    setDraft(next)
    scheduleSave(next)
    return next
  }

  useEffect(() => {
    const templateId = searchParams.get('template')
    if (templateId && draftRef.current.template_id === templateId) return
    if (!templateId && draftRef.current.template_id) return
    let alive = true
    listProductionTemplates().then(result => {
      if (!alive) return
      const template = templateId
        ? (result?.items || []).find(item => item.template_id === templateId)
        : (result?.items || []).find(item => item.is_default)
      if (!template) return
      patchDraft({
        template_id: template.template_id,
        text_style: template.text_style,
        visual_style: template.visual_style,
        ratio: template.ratio,
        template_voice_type: template.voice_type,
        template_tts_options: template.tts_options,
        subtitle_options: template.subtitle_options,
        generation_options: template.generation_options,
      })
      if (templateId) toast.success(`已应用生产模板“${template.name}”`)
    })
    return () => { alive = false }
  }, [searchParams])

  const insertExample = () => {
    const direction = normalizeTitle(draftRef.current.name || draftRef.current.manuscript) || '普通人为什么越来越需要 AI 助手'
    const next = {
      input_mode: 'script',
      manuscript: exampleScript.replace('大脑如何影响我们的决策？', direction),
      name: draftRef.current.name || direction,
    }
    const result = patchDraft(next)
    toast.success('已插入一版可编辑示例文稿')
    return result
  }

  const createProductionTask = async ({ prepared, currentText, voiceType }) => {
    setStarting(true)
    try {
      const inputMode = prepared.input_mode === 'theme' ? 'theme' : 'script'
      const result = await createTask({
        name: prepared.name,
        theme: String(currentText).slice(0, 5000),
        input_mode: inputMode,
        style: `${prepared.text_style || '知识科普'}|${prepared.visual_style || '吉卜力'}`,
        ratio: prepared.ratio || '16:9',
        length: inputMode === 'theme' ? normalizeLength(prepared.length) : 0,
        voice_type: prepared.template_voice_type || voiceType || undefined,
        tts_options: prepared.template_tts_options || readLastTtsOptions(),
        execution_mode: 'review_first',
        script_policy: inputMode === 'script' ? 'verbatim' : 'rewrite',
        source_draft_id: prepared.draft_id,
        template_id: prepared.template_id || undefined,
        generation_options: prepared.generation_options || undefined,
        subtitle_options: prepared.subtitle_options || undefined,
      })
      const saved = { ...prepared, created_task_id: result.task_id }
      draftRef.current = saved
      persistDraft(saved, setDraft, setSaveState, setSavedAt)
      localStorage.setItem('insightcut:last-workspace', JSON.stringify({ taskId: result.task_id, name: prepared.name, path: `/workspace/${result.task_id}` }))
      window.dispatchEvent(new Event('insightcut:workspace'))
      toast.success('正在生成预案')
      navigate(`/workspace/${result.task_id}`)
    } catch (error) {
      toast.error(error?.response?.data?.detail || '创建预案失败')
    } finally {
      setStarting(false)
    }
  }

  const continueToProduction = async () => {
    let next = draftRef.current
    if (next.input_mode !== 'theme' && !String(next.manuscript || '').trim()) next = insertExample()
    const currentText = next.input_mode === 'theme' ? next.theme : next.manuscript
    if (!String(currentText || '').trim()) {
      toast.warning(next.input_mode === 'theme' ? '请先输入视频主题' : '请先输入文稿内容')
      return
    }
    const name = String(next.name || '').trim() || normalizeTitle(currentText)
    const prepared = { ...next, name, length: next.input_mode === 'theme' ? normalizeLength(next.length) : next.length }
    const voiceType = localStorage.getItem('insightcut:last-voice') || ''
    const pendingLaunch = { prepared, currentText, voiceType }
    draftRef.current = prepared
    persistDraft(prepared, setDraft, setSaveState, setSavedAt)
    setStarting(true)
    try {
      const readiness = await getConfigReadiness({ voiceType })
      if (readiness?.status === 'not_ready' || readiness?.status === 'unknown') {
        setReadinessPrompt({ ...readiness, pendingLaunch })
        return
      }
      await createProductionTask(pendingLaunch)
    } catch {
      setReadinessPrompt({
        status: 'unknown',
        can_continue: true,
        items: [],
        pendingLaunch,
        message: '暂时无法确认模型服务是否可用。这不一定是配置错误，也可能是后端暂时断开。',
      })
    } finally {
      setStarting(false)
    }
  }

  const importDocument = async event => {
    const file = event.target.files?.[0]
    if (!file) return
    try {
      const result = await extractDocumentText(file)
      const imported = String(result?.text || '')
      if (!imported.trim()) {
        toast.warning('未能从文档中提取到可用文字')
        return
      }
      patchDraft({
        input_mode: 'script',
        manuscript: imported.slice(0, 5000),
        name: draftRef.current.name || file.name.replace(/\.(txt|md|markdown|docx|pdf)$/i, '').slice(0, 100),
      })
      toast.success(imported.length > 5000 ? '文档已导入，已截取前 5000 字' : '文档已导入文稿画布')
    } catch (error) {
      toast.error(error?.response?.data?.detail || '导入文档失败，请使用 TXT、Markdown、DOCX 或 PDF')
    } finally {
      event.target.value = ''
    }
  }

  const pasteFromClipboard = async () => {
    try {
      const pasted = await navigator.clipboard?.readText?.()
      if (!pasted) return toast.info('剪贴板暂无可粘贴文本')
      patchDraft({ input_mode: 'script', manuscript: pasted.slice(0, 5000), name: draftRef.current.name || normalizeTitle(pasted) })
      toast.success('已粘贴到文稿画布')
    } catch {
      toast.warning('浏览器未授权读取剪贴板')
    }
  }

  const rotating = rotatorItems[rotatorIndex]
  const draftSummary = useMemo(() => isTheme
    ? { label: '扩写目标', value: `${targetLength} 字`, description: '提交生产后会先扩写成完整文稿，再拆分分镜和计算时长。' }
    : { label: '文稿统计', value: `${estimateDuration(text, draft.voice_speed)}`, description: `预计 ${estimateSegments(text)} 段分镜，当前 ${contentLength} 个正文字符。` }, [contentLength, draft.voice_speed, isTheme, targetLength, text])

  return (
    <main className="creation-page manuscript-page">
      <section className="manuscript-layout">
        <aside className="work-panel project-panel" aria-label="项目控制">
          <PanelHeading eyebrow="项目设置" title="文稿准备" />
          <label className="field-label">项目名称
            <input value={draft.name || ''} maxLength="100" placeholder="给这条视频起个名字" onChange={event => patchDraft({ name: event.target.value })} />
            <small>{String(draft.name || '').length}/100</small>
          </label>
          <fieldset className="control-group"><legend>创作方式</legend>
            <div className="segmented-control" aria-label="创作方式">
              <button type="button" className={isTheme ? 'is-selected' : ''} onClick={() => patchDraft({ input_mode: 'theme' })}>主题模式</button>
              <button type="button" className={!isTheme ? 'is-selected' : ''} onClick={() => patchDraft({ input_mode: 'script' })}>脚本模式</button>
            </div>
            <p>{isTheme ? '输入一句主题，由模型扩写成完整视频文稿。' : '直接使用你输入或导入的完整文稿生产视频。'}</p>
          </fieldset>
          {isTheme ? <fieldset className="control-group"><legend>扩写字数</legend>
            <div className="length-control">
              <input aria-label="扩写字数滑块" type="range" min="0" max="2000" step="50" value={normalizeLength(draft.length)} onChange={event => patchDraft({ length: Number(event.target.value) })} />
              <label className="length-number"><span className="sr-only">扩写字数</span><input type="number" min="0" max="2000" step="50" value={draft.length === 0 ? '' : normalizeLength(draft.length)} placeholder="自动" onChange={event => patchDraft({ length: event.target.value === '' ? 0 : normalizeLength(event.target.value) })} onBlur={() => patchDraft({ length: normalizeLength(draftRef.current.length) })} /></label>
            </div>
            <small>0 为自动，手动范围 50-2000 字。</small>
          </fieldset> : <>
            <fieldset className="control-group"><legend>内容来源</legend>
              <div className="button-row"><button type="button" className="button button-secondary" onClick={insertExample}><Sparkles size={16} />插入示例</button><button type="button" className="button button-secondary" onClick={() => documentInput.current?.click()}><FileUp size={16} />导入文档</button></div>
              <small>支持 TXT、Markdown、Word DOCX、PDF</small>
              <input ref={documentInput} type="file" hidden accept=".txt,.md,.markdown,.docx,.pdf,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/pdf" onChange={importDocument} />
            </fieldset>
            <div className="button-row"><button type="button" className="button button-secondary" onClick={pasteFromClipboard}><ClipboardPaste size={16} />粘贴文本</button><button type="button" className="button button-secondary" onClick={() => patchDraft({ manuscript: '' })}>清空文稿</button></div>
          </>}
          <section className="summary-strip"><span>{draftSummary.label}</span><strong>{draftSummary.value}</strong><p>{draftSummary.description}</p></section>
        </aside>

        <section className="writing-canvas" aria-label="文稿编辑画布">
          <header className="canvas-heading"><div><p>{isTheme ? '主题模式' : '脚本画布'}</p><h1>{isTheme ? '主题输入' : '文稿编辑'}</h1></div><span className={`save-indicator ${saveState}`}><CheckCircle2 size={16} />{saveState === 'saved' ? `已保存 ${savedAt.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : '保存中...'}</span></header>
          <div className={`paper-editor-shell ${isEmpty ? 'is-empty' : ''}`}>
            <textarea ref={editorRef} value={text || ''} maxLength={isTheme ? 100 : 5000} placeholder={paperFocused ? (isTheme ? '输入 100 字以内的视频主题，例如：为什么普通人越来越需要 AI 助手' : '直接输入或粘贴完整文稿') : ''} onFocus={() => setPaperFocused(true)} onBlur={() => setPaperFocused(false)} onChange={event => patchDraft(isTheme ? { theme: event.target.value.slice(0, 100) } : { manuscript: event.target.value.slice(0, 5000) })} />
            {isEmpty ? <button type="button" className="empty-prompt" aria-label={isTheme ? '开始输入视频主题' : '开始输入视频文稿'} onClick={() => { setPaperFocused(true); editorRef.current?.focus() }}>
              <span className="empty-prompt-placeholder">{isTheme ? '输入 100 字以内主题...' : '在这里输入或粘贴完整文稿...'}</span>
              <EmptyStateCard
                as="span"
                variant="manuscript"
                eyebrow={isTheme ? '主题起稿' : '脚本起稿'}
                title={isTheme ? '从一个主题开始，也能把每一步看清楚' : '从一份文稿，到一条可继续编辑的视频'}
                description={isTheme ? '写下方向，完整文稿会在生成后先交给你确认。' : '粘贴或导入原文，脚本内容不会被自动改写。'}
                className="empty-onboarding"
              >
                <span className="empty-onboarding-steps" aria-label="三步制作流程">
                  <span><b>01</b><em>写文稿</em><small>输入主题或完整脚本</small></span>
                  <span><b>02</b><em>审预案</em><small>确认分镜、画面与音色</small></span>
                  <span><b>03</b><em>做成片</em><small>生成素材并按需导出</small></span>
                </span>
                <span className="opening-rotator-stage">
                  {rotating.type === 'slots' ? <span key={`slot-${rotatorIndex}`} className="opening-slot-group opening-anim-slot">{['文稿', '分镜', '成片', '剪映'].map((word, index) => <span key={word} className="opening-slot-item"><span className="opening-slot-word" style={{ animationDelay: `${index * 100}ms` }}>{word}</span></span>)}</span> : null}
                  {rotating.type === 'finale' ? <span key={`finale-${rotatorIndex}`} className="opening-rotator-text opening-text-finale opening-anim-finale">All in one <i>但不</i>是画布</span> : null}
                  {rotating.type === 'text' ? <span key={`text-${rotatorIndex}`} className={`opening-rotator-text ${rotating.visualClass} ${rotating.motionClass}`}>{rotating.text}</span> : null}
                </span>
              </EmptyStateCard>
            </button> : null}
            <footer><span>{isTheme ? '主题字数' : '字数'}：{contentLength}</span><span>自动保存到本地草稿</span></footer>
          </div>
        </section>

        <aside className="work-panel production-options" aria-label="生产设置">
          <PanelHeading eyebrow="生产设置" title="画面设置" />
          <fieldset className="control-group"><legend>画面风格</legend><div className="style-thumbnail-grid">{visualStyles.map(style => <VisualStyleCard key={style.value} style={style} selected={draft.visual_style === style.value} onSelect={value => patchDraft({ visual_style: value })} />)}</div></fieldset>
          <fieldset className="control-group"><legend>视频比例</legend><div className="segmented-control">{ratioOptions.map(ratio => <button type="button" key={ratio} className={draft.ratio === ratio ? 'is-selected' : ''} onClick={() => patchDraft({ ratio })}>{ratio}</button>)}</div></fieldset>
          <label className="field-label">创作风格<select value={draft.text_style || '知识科普'} onChange={event => patchDraft({ text_style: event.target.value })}>{textStyles.map(style => <option key={style}>{style}</option>)}</select></label>
          <button type="button" className="button button-primary continue-button" disabled={starting} onClick={continueToProduction}>{starting ? '正在创建预案…' : !isTheme && !String(draft.manuscript || '').trim() ? '插入示例并继续' : '生成预案'}</button>
        </aside>
      </section>
      <Modal
        open={Boolean(readinessPrompt)}
        title={readinessPrompt?.status === 'not_ready' ? '生成配置还未就绪' : '暂时无法确认服务状态'}
        onClose={() => setReadinessPrompt(null)}
        footer={readinessPrompt?.status === 'not_ready' ? <>
          <button type="button" className="button button-secondary" onClick={() => setReadinessPrompt(null)}>稍后处理</button>
          <button type="button" className="button button-primary" onClick={() => {
            const firstMissing = readinessPrompt.items?.find(item => item.status === 'not_ready')
            setReadinessPrompt(null)
            navigate(`/settings#${firstMissing?.settings_anchor || 'settings-llm'}`)
          }}>打开 API 配置</button>
        </> : <>
          <button type="button" className="button button-secondary" onClick={() => setReadinessPrompt(null)}>取消</button>
          <button type="button" className="button button-primary" disabled={starting} onClick={async () => {
            const pendingLaunch = readinessPrompt?.pendingLaunch
            setReadinessPrompt(null)
            if (pendingLaunch) await createProductionTask(pendingLaunch)
          }}>仍然继续</button>
        </>}
      >
        <div className="readiness-dialog-content">
          <p>{readinessPrompt?.message || (readinessPrompt?.status === 'not_ready'
            ? '下列当前项目必需的配置明确缺失，因此还没有创建项目。'
            : '配置看起来已填写，但运行时可用性需在生成时确认。')}</p>
          {readinessPrompt?.items?.filter(item => item.status !== 'ready').length ? <ul>
            {readinessPrompt.items.filter(item => item.status !== 'ready').map(item => <li key={item.key}>
              <strong>{item.label}</strong>
              <span>{item.missing?.length ? `缺少：${item.missing.join('、')}` : item.message}</span>
            </li>)}
          </ul> : null}
        </div>
      </Modal>
    </main>
  )
}

function persistDraft(draft, setDraft, setSaveState, setSavedAt) {
  if (!draft?.draft_id) return
  const saved = saveDraft(draft)
  setDraft(saved)
  setSaveState('saved')
  setSavedAt(new Date())
}

function PanelHeading({ eyebrow, title }) {
  return <header className="panel-heading"><p>{eyebrow}</p><h2>{title}</h2></header>
}
