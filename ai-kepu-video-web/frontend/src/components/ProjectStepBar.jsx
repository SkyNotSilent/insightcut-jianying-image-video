import { Check } from 'lucide-react'
import { useNavigate } from 'react-router'

const STEPS = [
  ['写文稿', '来源文稿'],
  ['生成预案', '文案与提示词'],
  ['确认音色', '全片配音'],
  ['确认画面', '逐段检查'],
  ['生成素材', '图片与音频'],
  ['完成导出', '三种交付'],
]

export function ProjectStepBar({ taskId, currentStep = 6, reachedStep = 6 }) {
  const navigate = useNavigate()
  const openStep = index => {
    const step = index + 1
    if (step > reachedStep) return
    if (step === 6) navigate(`/export/${taskId}`)
    else navigate(`/workspace/${taskId}?focus=${step}`)
  }

  return <nav className="project-step-bar" aria-label="项目生产步骤">
    <ol>{STEPS.map(([label, description], index) => {
      const step = index + 1
      const completed = step < currentStep
      return <li key={label} className={`${step === currentStep ? 'is-current' : ''}${completed ? ' is-completed' : ''}`}>
        <button type="button" disabled={step > reachedStep} onClick={() => openStep(index)}>
          <span>{completed ? <Check size={12} /> : String(step).padStart(2, '0')}</span>
          <span><strong>{label}</strong><small>{description}</small></span>
        </button>
      </li>
    })}</ol>
  </nav>
}
