import { useState } from 'react'
import { CircleHelp } from 'lucide-react'
import { Tooltip } from './ui/Tooltip'

export function WorkspaceStageNavigator({ journey, onHelp, onNavigate }) {
  const [expanded, setExpanded] = useState(false)
  const current = journey.steps.find(step => step.state === 'current') || journey.steps[journey.steps.length - 1]
  return <section className="workspace-stage-navigation" aria-label="项目阶段与总体进度">
    <button type="button" className="workspace-mobile-stage" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>当前：{current?.label} · {expanded ? '收起步骤' : '查看全部步骤'}</button>
    <ol className={expanded ? 'is-expanded' : ''}>
      {journey.steps.map((step, index) => <li key={step.id} className={`is-${step.state}`} aria-current={step.state === 'current' ? 'step' : undefined}>
        <button type="button" disabled={step.state === 'pending'} onClick={() => onNavigate?.(step, index)} aria-label={`${String(index + 1).padStart(2, '0')} ${step.label}`}>
        <span>{String(index + 1).padStart(2, '0')}</span>
        <div><strong>{step.label}</strong><small>{step.description}</small></div>
        </button>
      </li>)}
    </ol>
    <div className="workspace-global-progress">
      <span><strong>{journey.percent}%</strong><small>{journey.estimateLabel}</small>{onHelp ? <Tooltip label="查看工作台使用引导" placement="bottom"><button type="button" aria-label="查看工作台使用引导" onClick={onHelp}><CircleHelp size={14} aria-hidden="true" /></button></Tooltip> : null}</span>
      <progress max="100" value={journey.percent} aria-label={`总体进度 ${journey.percent}%`} />
    </div>
  </section>
}
