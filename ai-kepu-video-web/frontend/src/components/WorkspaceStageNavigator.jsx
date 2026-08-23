import { CircleHelp } from 'lucide-react'
import { Tooltip } from './ui/Tooltip'

export function WorkspaceStageNavigator({ journey, onHelp, onNavigate }) {
  return <section className="workspace-stage-navigation" aria-label="项目阶段与总体进度">
    <ol>
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
