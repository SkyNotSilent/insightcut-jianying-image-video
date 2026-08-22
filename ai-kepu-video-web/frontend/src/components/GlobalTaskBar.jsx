import { ChevronDown, CircleAlert, LoaderCircle, MoreHorizontal, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { cancelTask, getTaskActivity } from '../api/task'
import { usePollingResource } from '../hooks/usePollingResource'
import { toast } from '../lib/toast'

const STEP_LABELS = ['写文稿', '生成预案', '确认音色', '确认画面', '生成素材', '完成导出']

function TaskCard({ task, onOpen, onCancel }) {
  const attention = ['awaiting_confirmation', 'awaiting_finalization', 'interrupted', 'failed'].includes(task.status)
  return <article className={`task-activity-card${attention ? ' needs-attention' : ''}`}>
    <button type="button" className="task-activity-card-main" onClick={() => onOpen(task)}>
      <span className="task-activity-card-icon" aria-hidden="true">
        {attention ? <CircleAlert size={15} /> : <LoaderCircle size={15} />}
      </span>
      <span className="task-activity-card-copy">
        <strong>{task.name}</strong>
        <small>{String(task.step || 1).padStart(2, '0')} · {STEP_LABELS[(task.step || 1) - 1] || task.stage}</small>
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

  const poll = usePollingResource({
    resourceKey: 'global-task-activity',
    request: (_, { signal }) => getTaskActivity({ signal }),
    interval: 3000,
    onData: data => setActivity(data || { running: [], attention: [], recent: [], counts: { running: 0, attention: 0 } }),
  })

  const visibleTasks = useMemo(
    () => [...(activity.running || []), ...(activity.attention || []), ...(activity.recent || []).slice(0, 4)],
    [activity],
  )
  const mostRecent = visibleTasks[0]

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

  return <section className={`global-task-bar${expanded ? ' is-expanded' : ''}`} aria-label="全局任务活动">
    <button type="button" className="global-task-capsule" onClick={() => setExpanded(value => !value)} aria-expanded={expanded}>
      <span className={`task-live-dot${activity.counts?.running ? ' is-live' : ''}`} aria-hidden="true" />
      <strong>运行 {activity.counts?.running || 0} 项</strong>
      <span>待处理 {activity.counts?.attention || 0} 项</span>
      {mostRecent ? <small>{mostRecent.name}</small> : <small>暂无活动任务</small>}
      <ChevronDown size={15} aria-hidden="true" />
    </button>
    {expanded ? <div className="global-task-drawer">
      <div className="global-task-drawer-heading">
        <div><strong>任务活动</strong><span>项目切换不会中断后台生成</span></div>
        <button type="button" className="icon-button" onClick={() => setExpanded(false)} aria-label="收起任务活动"><X size={16} /></button>
      </div>
      {visibleTasks.length ? <div className="global-task-track">
        {visibleTasks.map(task => <TaskCard key={task.task_id} task={task} onOpen={item => {
          setExpanded(false)
          navigate(location.pathname.startsWith('/export/')
            ? `/export/${item.task_id}`
            : item.target_route || `/workspace/${item.task_id}`)
        }} onCancel={handleCancel} />)}
      </div> : <div className="global-task-empty">暂无运行或待处理任务。可以从文稿页开始一个新项目。</div>}
    </div> : null}
  </section>
}
