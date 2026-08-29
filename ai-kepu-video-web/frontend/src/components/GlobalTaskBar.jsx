import { Check, CheckCircle2, ChevronRight, CircleAlert, LoaderCircle, MoreHorizontal, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { cancelTask, getTaskActivity } from '../api/task'
import { usePollingResource } from '../hooks/usePollingResource'
import { PROJECT_SELECTION_EVENT, projectIdFromPath, readSelectedProject, selectProject } from '../lib/projectSelection'
import { toast } from '../lib/toast'

const STEP_LABELS = ['写文稿', '生成预案', '确认音色', '确认画面', '生成素材', '完成导出']
const HIDDEN_ACTIVITY_STATUSES = new Set(['cancelled', 'deleting'])

function isVisibleActivityTask(task) {
  if (!task?.task_id || HIDDEN_ACTIVITY_STATUSES.has(task.status) || task.exported_at) return false
  if (task.status === 'completed') return task.export_ready !== false
  return true
}

function TaskCard({ task, selected, onOpen, onCancel }) {
  const exportReady = task.export_ready === true || (task.status === 'completed' && !task.exported_at)
  const attention = !exportReady && ['awaiting_confirmation', 'awaiting_finalization', 'interrupted', 'failed'].includes(task.status)
  return <article className={`task-activity-card${attention ? ' needs-attention' : ''}${exportReady ? ' is-export-ready' : ''}${selected ? ' is-selected' : ''}`}>
    <button type="button" className="task-activity-card-main" aria-pressed={selected} onClick={() => onOpen(task)}>
      <span className="task-activity-card-icon" aria-hidden="true">
        {exportReady ? <CheckCircle2 size={15} /> : attention ? <CircleAlert size={15} /> : <LoaderCircle size={15} />}
      </span>
      <span className="task-activity-card-copy">
        <strong>{task.name}</strong>
        <small>{exportReady ? '06 · 可导出' : `${String(task.step || 1).padStart(2, '0')} · ${task.activity_label || STEP_LABELS[(task.step || 1) - 1] || task.stage}`}{selected ? <em><Check size={10} />当前项目</em> : null}</small>
      </span>
      <span className="task-activity-card-percent">{task.progress}%</span>
      <span className="task-activity-card-progress"><i style={{ width: `${task.progress}%` }} /></span>
    </button>
    {task.can_cancel ? <details className="task-activity-menu">
      <summary aria-label={`${task.name} 更多操作`}><MoreHorizontal size={15} /></summary>
      <button type="button" onClick={() => onCancel(task)}><X size={14} /> 取消任务</button>
    </details> : null}
  </article>
}

export function GlobalTaskBar() {
  const navigate = useNavigate()
  const location = useLocation()
  const [activity, setActivity] = useState({ running: [], attention: [], recent: [], counts: { running: 0, attention: 0 } })
  const [selectedProject, setSelectedProject] = useState(readSelectedProject)
  const trackRef = useRef(null)
  const routeTaskId = projectIdFromPath(location.pathname)

  const poll = usePollingResource({
    resourceKey: 'global-task-activity',
    request: (_, { signal }) => getTaskActivity({ signal }),
    interval: 3000,
    onData: data => setActivity(data || { running: [], attention: [], recent: [], counts: { running: 0, attention: 0 } }),
  })

  const allTasks = useMemo(() => {
    const unique = new Map()
    ;[...(activity.running || []), ...(activity.attention || []), ...(activity.recent || [])]
      .forEach(task => { if (task?.task_id && !unique.has(task.task_id)) unique.set(task.task_id, task) })
    return [...unique.values()]
  }, [activity])
  const visibleTasks = useMemo(
    () => {
      const primary = [...(activity.running || []), ...(activity.attention || []), ...(activity.recent || [])]
      const unique = [...new Map(primary.filter(isVisibleActivityTask).map(task => [task.task_id, task])).values()]
      const selectedTask = allTasks.find(task => task.task_id === selectedProject?.taskId && isVisibleActivityTask(task))
      return selectedTask ? [selectedTask, ...unique.filter(task => task.task_id !== selectedTask.task_id)] : unique
    },
    [activity, allTasks, selectedProject?.taskId],
  )

  useEffect(() => {
    const refresh = event => setSelectedProject(event?.detail === null ? null : readSelectedProject())
    window.addEventListener('storage', refresh)
    window.addEventListener(PROJECT_SELECTION_EVENT, refresh)
    return () => {
      window.removeEventListener('storage', refresh)
      window.removeEventListener(PROJECT_SELECTION_EVENT, refresh)
    }
  }, [])

  useEffect(() => {
    if (!routeTaskId) return
    const task = allTasks.find(item => item.task_id === routeTaskId)
    const current = readSelectedProject()
    const name = task?.name || (current?.taskId === routeTaskId ? current.name : '')
    if (current?.taskId === routeTaskId && current.name === name) return
    const next = selectProject({ taskId: routeTaskId, name })
    if (next) setSelectedProject(next)
  }, [allTasks, routeTaskId])

  const handleCancel = async task => {
    if (!window.confirm(`确认取消“${task.name}”？已经生成的素材会继续保留。`)) return
    try {
      await cancelTask(task.task_id)
      toast.info('已提交取消请求，已有素材不会删除')
      poll.refresh()
    } catch {
      // request interceptor already reports the failure
    }
  }

  const handleOpen = task => {
    const next = selectProject(task)
    if (next) setSelectedProject(next)
    navigate(location.pathname.startsWith('/export/')
      ? `/export/${task.task_id}`
      : location.pathname.startsWith('/workspace/')
        ? `/workspace/${task.task_id}`
        : task.target_route || `/workspace/${task.task_id}`)
  }

  const browseMore = () => {
    const track = trackRef.current
    if (!track) return
    const atEnd = track.scrollLeft + track.clientWidth >= track.scrollWidth - 8
    track.scrollTo({ left: atEnd ? 0 : track.scrollLeft + Math.min(224, track.clientWidth * .8), behavior: 'smooth' })
  }

  return <section className="global-task-bar" aria-label="全局任务活动">
    <div className="global-task-drawer">
      <strong className="global-task-drawer-label"><span className={`task-live-dot${activity.counts?.running ? ' is-live' : ''}`} aria-hidden="true" />进行中 {visibleTasks.length}</strong>
      {visibleTasks.length ? <div ref={trackRef} className="global-task-track">
        {visibleTasks.map(task => <TaskCard key={task.task_id} task={task} selected={task.task_id === selectedProject?.taskId} onOpen={handleOpen} onCancel={handleCancel} />)}
      </div> : <div className="global-task-empty">暂无进行中或待处理项目。可以从文稿页开始一个新项目。</div>}
      <button type="button" className="icon-button global-task-drawer-next" onClick={browseMore} aria-label="浏览更多项目" title="浏览更多项目" disabled={visibleTasks.length < 2}><ChevronRight size={16} /></button>
    </div>
  </section>
}
