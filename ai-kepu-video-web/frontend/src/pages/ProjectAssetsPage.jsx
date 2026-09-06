import { useCallback, useEffect, useMemo, useState, useRef } from 'react'
import { ArrowRight, FileText, ImageOff, MoreHorizontal, Plus, RotateCcw, Search, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router'
import { deleteTask, getProjectCatalog } from '../api/task'
import { ConfirmDialog } from '../components/Modal'
import { EmptyState, LoadingState } from '../components/StatusStates'
import { toast } from '../lib/toast'
import { clearSelectedProject } from '../lib/projectSelection'
import { createDraft, deleteDraft, estimateDuration, formatLocalTime, listDrafts, visualStyles } from '../utils/projectDrafts'
import { normalizeMediaUrl } from '../utils/mediaUrl'
import { deriveTaskState } from '../utils/taskState'
import { getDeleteConfirmation, getDeletionIssueCount, getProjectPrimaryAction } from './projectActions'
import './delivery-pages.css'

const STATUS_FILTERS = [
  { key: 'all', label: '全部项目', tone: 'info' },
  { key: 'draft', label: '草稿', tone: 'warning' },
  { key: 'waiting', label: '待确认', tone: 'warning' },
  { key: 'processing', label: '生成中', tone: 'info' },
  { key: 'interrupted', label: '可继续', tone: 'warning' },
  { key: 'completed', label: '已完成', tone: 'success' },
  { key: 'recoverable_assets', label: '失败可恢复', tone: 'danger' },
]
const DURATION_FILTERS = ['全部时长', '1 分钟以内', '1-3 分钟', '3-5 分钟', '5 分钟以上']
const DEFAULT_VISIBLE_STATUSES = new Set(['waiting', 'processing', 'interrupted', 'completed', 'export_ready', 'recoverable_assets'])

function secondsToLabel(value) {
  const seconds = Math.round(Number(value) || 0)
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function durationToSeconds(label) {
  const [minutes = 0, seconds = 0] = String(label || '').split(':').map(Number)
  return (Number(minutes) || 0) * 60 + (Number(seconds) || 0)
}

function matchesDuration(seconds, filter) {
  if (!Number.isFinite(seconds) || seconds <= 0) return filter === '全部时长'
  if (filter === '1 分钟以内') return seconds < 60
  if (filter === '1-3 分钟') return seconds >= 60 && seconds < 180
  if (filter === '3-5 分钟') return seconds >= 180 && seconds < 300
  if (filter === '5 分钟以上') return seconds >= 300
  return true
}

function firstSegmentCover(segments) {
  return [...(Array.isArray(segments) ? segments : [])]
    .sort((a, b) => Number(a?.segment_index ?? 0) - Number(b?.segment_index ?? 0))
    .map(segment => normalizeMediaUrl(segment?.image_url))
    .find(Boolean) || ''
}

export function ProjectAssetsPage() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [styleFilter, setStyleFilter] = useState('')
  const [durationFilter, setDurationFilter] = useState('全部时长')
  const [sortMode, setSortMode] = useState('updated')
  const [remoteTasks, setRemoteTasks] = useState([])
  const taskSegments = {}
  const [page, setPage] = useState(1)
  const [catalog, setCatalog] = useState({ total: 0, counts: {} })
  const [loadError, setLoadError] = useState('')
  const loadSequence = useRef(0)
  const [localDrafts, setLocalDrafts] = useState([])
  const [brokenCovers, setBrokenCovers] = useState({})
  const [fallbackCovers, setFallbackCovers] = useState({})
  const [openMenuId, setOpenMenuId] = useState(null)
  const [projectToDelete, setProjectToDelete] = useState(null)
  const [deletingId, setDeletingId] = useState(null)

  const loadProjects = useCallback(async () => {
    const sequence = ++loadSequence.current
    setLoading(true)
    setLoadError('')
    setLocalDrafts(listDrafts())
    try {
      if (statusFilter === 'draft') return
      const durations = { '1 分钟以内': 'under1', '1-3 分钟': '1to3', '3-5 分钟': '3to5', '5 分钟以上': 'over5' }
      const result = await getProjectCatalog({ page, limit: 40, q: search, status: statusFilter, style: styleFilter, duration: durations[durationFilter] || '', sort: sortMode })
      if (sequence !== loadSequence.current) return
      setRemoteTasks(result.items || [])
      setCatalog(result)
      if (page > 1 && !result.items?.length && result.total > 0) setPage(Math.max(1, Math.ceil(result.total / 40)))
    } catch (error) {
      if (sequence !== loadSequence.current) return
      setLoadError('加载本地项目失败，请重试；本地文稿仍可使用')
      toast.error('加载本地项目失败，请重试')
    } finally {
      if (sequence === loadSequence.current) setLoading(false)
    }
  }, [page, search, statusFilter, styleFilter, durationFilter, sortMode])

  useEffect(() => {
    const timer = window.setTimeout(loadProjects, 180)
    return () => { window.clearTimeout(timer); loadSequence.current += 1 }
  }, [loadProjects])

  useEffect(() => {
    if (!remoteTasks.some(task => task.status === 'deleting')) return
    const timer = window.setInterval(loadProjects, 1500)
    return () => window.clearInterval(timer)
  }, [remoteTasks, loadProjects])

  const changeFilter = setter => value => { setPage(1); setter(value) }

  const projects = useMemo(() => {
    const drafts = localDrafts
      .filter(draft => draft.name?.trim() || draft.manuscript?.trim() || draft.theme?.trim())
      .map(draft => {
        const duration = estimateDuration(draft.manuscript || draft.theme)
        return {
          id: draft.draft_id,
          type: 'draft',
          name: draft.name || draft.theme || '未命名文稿',
          status: 'draft',
          statusLabel: '草稿',
          tone: 'warning',
          provider: '本地文稿',
          duration,
          durationSeconds: durationToSeconds(duration),
          updatedAt: formatLocalTime(draft.updated_at),
          sortTime: draft.updated_at || '',
          cover: '',
          visualStyle: draft.visual_style || '',
        }
      })
    const tasks = remoteTasks.map(task => {
      const segments = taskSegments[task.task_id] || []
      const state = task.display_state ? { key: task.display_state, label: task.display_label, tone: task.display_tone, actionLabel: task.action_label } : deriveTaskState({ task, segments })
      const durationSeconds = taskDurationSeconds(task, segments)
      return {
        id: task.task_id,
        type: 'task',
        name: task.name || task.result?.theme || task.theme || `视频项目 ${task.task_id?.slice(0, 6)}`,
        status: state.key,
        statusLabel: state.label,
        tone: state.tone,
        actionLabel: state.actionLabel,
        provider: task.voice_type ? `TTS · ${task.voice_type}` : '生成项目',
        duration: durationSeconds ? secondsToLabel(durationSeconds) : '--:--',
        durationSeconds,
        updatedAt: formatLocalTime(task.updated_at || task.created_at || task.result?.created_at),
        sortTime: task.updated_at || task.created_at || task.result?.created_at || '',
        cover: normalizeMediaUrl(task.cover_image_url || fallbackCovers[task.task_id] || ''),
        visualStyle: task.visual_style || task.result?.visual_style || '',
      }
    })
    return [...drafts, ...tasks]
  }, [fallbackCovers, localDrafts, remoteTasks, taskSegments])

  const filteredProjects = useMemo(() => {
    const keyword = search.trim().toLowerCase()
    return projects
      .filter(project => {
        if (project.type === 'task') return statusFilter !== 'draft'
        if (statusFilter === 'draft') return project.type === 'draft'
        if (project.type === 'draft') return false
        if (statusFilter === 'all') return DEFAULT_VISIBLE_STATUSES.has(project.status)
        if (statusFilter === 'completed') return project.status === 'completed' || project.status === 'export_ready'
        return project.status === statusFilter
      })
      .filter(project => project.type === 'task' || !styleFilter || project.visualStyle === styleFilter)
      .filter(project => project.type === 'task' || matchesDuration(project.durationSeconds, durationFilter))
      .filter(project => project.type === 'task' || !keyword || project.name.toLowerCase().includes(keyword) || project.provider.toLowerCase().includes(keyword))
      .sort((a, b) => {
        if (a.type === "task" && b.type === "task") return 0
        if (sortMode === 'name') return a.name.localeCompare(b.name, 'zh-CN')
        if (sortMode === 'status') return a.status.localeCompare(b.status)
        return String(b.sortTime).localeCompare(String(a.sortTime))
      })
  }, [durationFilter, projects, search, sortMode, statusFilter, styleFilter])

  const statusCount = key => key === 'draft' ? projects.filter(project => project.type === 'draft').length : (catalog.counts?.[key] || 0)
  const styleCount = () => undefined

  const resetFilters = () => {
    setPage(1)
    setStatusFilter('all')
    setStyleFilter('')
    setDurationFilter('全部时长')
    setSearch('')
  }

  const createProject = () => {
    const draft = createDraft()
    navigate(`/manuscript/${draft.draft_id}`)
  }

  const openProject = project => {
    const action = getProjectPrimaryAction(project)
    if (action === 'draft') { navigate(`/manuscript/${project.id}`); return }
    navigate(`/workspace/${project.id}`)
  }

  const selectDelete = project => {
    setOpenMenuId(null)
    setProjectToDelete(project)
  }

  const removeTaskFromView = taskId => {
    setRemoteTasks(current => current.filter(task => task.task_id !== taskId))
    setTaskSegments(current => {
      const next = { ...current }
      delete next[taskId]
      return next
    })
    setFallbackCovers(current => {
      const next = { ...current }
      delete next[taskId]
      return next
    })
    setBrokenCovers(current => {
      const next = { ...current }
      delete next[taskId]
      return next
    })
  }

  const confirmDelete = async () => {
    if (!projectToDelete || deletingId) return
    if (projectToDelete.type === 'draft') {
      deleteDraft(projectToDelete.id)
      setLocalDrafts(listDrafts())
      setProjectToDelete(null)
      toast.success('草稿已删除')
      return
    }

    setDeletingId(projectToDelete.id)
    try {
      const result = await deleteTask(projectToDelete.id, { deleteFiles: true })
      clearSelectedProject(projectToDelete.id)
      if (result?.outcome === "deleting") await loadProjects()
      else removeTaskFromView(projectToDelete.id)
      setProjectToDelete(null)
      const issueCount = getDeletionIssueCount(result)
      if (issueCount) toast.warning(`项目已删除，仍有 ${issueCount} 个本地路径未清理`)
      else if (result?.outcome === 'deleting') toast.info('项目正在停止生成，随后会自动完成删除')
      else toast.success('项目及本地素材已删除')
    } catch (error) {
      console.warn('删除项目失败', error)
      toast.error('删除项目失败，请重试')
    } finally {
      setDeletingId(null)
    }
  }

  const deleteConfirmation = getDeleteConfirmation(projectToDelete || {})

  const sectionTitle = STATUS_FILTERS.find(item => item.key === statusFilter)?.label || '全部项目'
  const isProjectLibraryEmpty = projects.length === 0

  return (
    <main className="assets-page">
      <aside className="assets-filters" aria-label="项目筛选">
        <div className="assets-filter-title"><h2>筛选条件</h2><button type="button" onClick={resetFilters}><RotateCcw size={14} aria-hidden="true" />重置</button></div>
        <FilterGroup label="项目状态" items={STATUS_FILTERS.map(item => ({ ...item, count: statusCount(item.key) }))} value={statusFilter} onChange={changeFilter(setStatusFilter)} />
        <FilterGroup label="视频风格" items={[{ key: '', label: '全部风格', count: statusCount('all') }, ...visualStyles.map(style => ({ key: style.value, label: style.label, count: styleCount(style.value) }))]} value={styleFilter} onChange={changeFilter(setStyleFilter)} />
        <FilterGroup label="时长" items={DURATION_FILTERS.map(item => ({ key: item, label: item }))} value={durationFilter} onChange={changeFilter(setDurationFilter)} />
      </aside>

      <section className="assets-workspace">
        <header className="assets-toolbar">
          <div><p className="eyebrow">项目资产</p><h1>{sectionTitle} <span>{statusFilter === "draft" ? filteredProjects.length : catalog.total}</span></h1></div>
          <div className="assets-toolbar-actions">
            <label className="assets-search"><Search size={16} aria-hidden="true" /><span className="sr-only">搜索项目</span><input value={search} onChange={event => changeFilter(setSearch)(event.target.value)} placeholder="搜索名称或音色" /></label>
            <select aria-label="项目排序" value={sortMode} onChange={event => changeFilter(setSortMode)(event.target.value)}><option value="updated">最近更新</option><option value="name">项目名称</option><option value="status">项目状态</option></select>
            <button className="button button-primary" type="button" onClick={createProject}><Plus size={16} aria-hidden="true" />新建文稿</button>
          </div>
        </header>

        {loading ? <LoadingState label="正在汇总本地草稿和项目..." /> : loadError ? null : filteredProjects.length === 0 ? <EmptyState variant="projects" eyebrow={isProjectLibraryEmpty ? '项目档案' : '筛选结果'} title={isProjectLibraryEmpty ? '还没有项目' : '没有匹配的项目'} description={isProjectLibraryEmpty ? '从一份文稿开始，后续的分镜、素材和导出会按项目归档在这里。' : '当前筛选条件下没有结果，可以调整左侧筛选，或直接开始新文稿。'} action={<button className="button button-primary" type="button" onClick={createProject}><Plus size={16} aria-hidden="true" />新建文稿</button>} /> : (
          <div className="asset-project-grid">
            {filteredProjects.map(project => {
              const usableCover = Boolean(project.cover && !brokenCovers[project.id])
              return (
                <article className="asset-project-card" key={project.id}>
                  <button className="asset-project-open" type="button" disabled={project.status === "deleting"} onClick={() => openProject(project)} aria-label={`打开 ${project.name}`}>
                    <div className={`asset-project-thumb${usableCover ? '' : ' is-empty'}`}>
                      {usableCover ? <img src={project.cover} alt="" onError={() => setBrokenCovers(current => ({ ...current, [project.id]: true }))} /> : <div><ImageOff size={22} aria-hidden="true" /><strong>{project.name.slice(0, 2)}</strong><small>{project.type === 'draft' ? '文稿草稿' : '暂无画面'}</small></div>}
                      <span>{project.duration}</span>
                    </div>
                    <div className="asset-project-copy">
                      <h2>{project.name}</h2>
                      <div className="asset-project-status"><span className={`status-pill is-${project.tone}`}>{project.statusLabel}</span><strong>{project.type === 'draft' ? '继续文稿' : project.actionLabel || '查看工作台'} <ArrowRight size={13} aria-hidden="true" /></strong></div>
                      <div className="asset-project-meta"><span>{project.provider}</span><time>{project.updatedAt}</time></div>
                    </div>
                  </button>
                  <div className="asset-project-actions">
                    <button className="asset-project-menu icon-button" type="button" disabled={project.status === "deleting"} title="项目操作" aria-label={`${project.name} 的项目操作`} aria-expanded={openMenuId === project.id} onClick={() => setOpenMenuId(current => current === project.id ? null : project.id)}><MoreHorizontal size={18} aria-hidden="true" /></button>
                    {openMenuId === project.id ? <div className="asset-project-popover" role="menu">{project.type === 'task' ? <button type="button" role="menuitem" onClick={() => navigate(`/assets/${project.id}`)}><FileText size={15} aria-hidden="true" />查看项目素材</button> : null}<button type="button" role="menuitem" onClick={() => selectDelete(project)}><Trash2 size={15} aria-hidden="true" />{project.type === 'draft' ? '删除草稿' : '删除项目'}</button></div> : null}
                  </div>
                </article>
              )
            })}
          </div>
        )}
        {loadError && <div role="alert">{loadError}<button onClick={loadProjects}>重试加载</button></div>}
        {!loading && statusFilter !== 'draft' && !loadError && <nav aria-label="项目分页" className="assets-pagination"><button disabled={page === 1} onClick={() => setPage(value => value - 1)}>上一页</button><span>第 {page} / {Math.max(1, Math.ceil(catalog.total / 40))} 页，共 {catalog.total} 项</span><button disabled={page * 40 >= catalog.total} onClick={() => setPage(value => value + 1)}>下一页</button></nav>}
        {!loading && statusFilter === 'draft' && filteredProjects.length > 0 && <footer className="assets-result-count"><FileText size={15} aria-hidden="true" />共 {filteredProjects.length} 项</footer>}
      </section>

      <ConfirmDialog open={Boolean(projectToDelete)} title={deleteConfirmation.title} message={deleteConfirmation.message} confirmLabel={deletingId ? '正在删除...' : deleteConfirmation.confirmLabel} confirmDisabled={Boolean(deletingId)} danger onConfirm={confirmDelete} onClose={() => { if (!deletingId) setProjectToDelete(null) }} />
    </main>
  )
}

function taskDurationSeconds(task, segments) {
  const directDuration = Number(task?.total_duration ?? task?.result?.total_duration)
  if (directDuration > 0) return directDuration
  return (Array.isArray(segments) ? segments : []).reduce((total, segment) => total + (Number(segment?.duration) || 0), 0)
}

function FilterGroup({ label, items, value, onChange }) {
  return (
    <section className="assets-filter-group">
      <h3>{label}</h3>
      {items.map(item => <button type="button" key={item.key} className={value === item.key ? 'is-active' : ''} onClick={() => onChange(item.key)}><span className={`filter-dot is-${item.tone || 'neutral'}`} aria-hidden="true" /><strong>{item.label}</strong>{item.count !== undefined && <em>{item.count}</em>}</button>)}
    </section>
  )
}
