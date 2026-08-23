import { Check, ChevronDown, CircleAlert, LoaderCircle, MoreHorizontal, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { cancelTask, getTaskActivity } from '../api/task'
import { usePollingResource } from '../hooks/usePollingResource'
import { PROJECT_SELECTION_EVENT, projectIdFromPath, readSelectedProject, selectProject } from '../lib/projectSelection'
import { toast } from '../lib/toast'

const STEP_LABELS = ['写文稿', '生成预案', '确认音色', '确认画面', '生成素材', '完成导出']

function TaskCard({ task, selected, onOpen, onCancel }) {
  const attention = ['awaiting_confirmation', 'awaiting_finalization', 'interrupted', 'failed'].includes(task.status)
  return <article className={`task-activity-card${attention ? ' needs-attention' : ''}${selected ? ' is-selected' : ''}`}>
    <button type="button" className="task-activity-card-main" aria-pressed={selected} onClick={() => onOpen(task)}>
      <span className="task-activity-card-icon" aria-hidden="true">
        {attention ? <CircleAlert size={15} /> : <LoaderCircle size={15} />}
      </span>
      <span className="task-activity-card-copy">
        <strong>{task.name}</strong>
        <small>{String(task.step || 1).padStart(2, '0')} · {STEP_LABELS[(task.step || 1) - 1] || task.stage}{selected ? <em><Check size={10} />当前项目</em> : null}</small>
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
  const [expanded, setExpanded] = useState(false)
  const [selectedProject, setSelectedProject] = useState(readSelectedProject)
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
      const primary = [...(activity.running || []), ...(activity.attention || []), ...(activity.recent || []).slice(0, 4)]
      const unique = [...new Map(primary.filter(Boolean).map(task => [task.task_id, task])).values()]
      const selectedTask = allTasks.find(task => task.task_id === selectedProject?.taskId)
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
    setExpanded(false)
    navigate(location.pathname.startsWith('/export/')
      ? `/export/${task.task_id}`
      : location.pathname.startsWith('/workspace/')
        ? `/workspace/${task.task_id}`
        : task.target_route || `/workspace/${task.task_id}`)
  }

  return <section className={`global-task-bar${expanded ? ' is-expanded' : ''}`} aria-label="全局任务活动">
    <button type="button" className="global-task-capsule" onClick={() => setExpanded(value => !value)} aria-expanded={expanded}>
      <span className={`task-live-dot${activity.counts?.running ? ' is-live' : ''}`} aria-hidden="true" />
      <strong>运行 {activity.counts?.running || 0} 项</strong>
      <span>待处理 {activity.counts?.attention || 0} 项</span>
      {selectedProject ? <span className="task-current-context"><em>当前</em>{selectedProject.name || `项目 ${selectedProject.taskId.slice(0, 6)}`}</span> : <small>未选择项目</small>}
      <ChevronDown size={15} aria-hidden="true" />
    </button>
    {expanded ? <div className="global-task-drawer">
      <div className="global-task-drawer-heading">
        <div><strong>任务活动</strong><span>项目切换不会中断后台生成</span></div>
        <button type="button" className="icon-button" onClick={() => setExpanded(false)} aria-label="收起任务活动"><X size={16} /></button>
      </div>
      {visibleTasks.length ? <div className="global-task-track">
        {visibleTasks.map(task => <TaskCard key={task.task_id} task={task} selected={task.task_id === selectedProject?.taskId} onOpen={handleOpen} onCancel={handleCancel} />)}
      </div> : <div className="global-task-empty">暂无运行或待处理任务。可以从文稿页开始一个新项目。</div>}
    </div> : null}
  </section>
}
