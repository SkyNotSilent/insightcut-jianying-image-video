import { CheckCircle2 } from 'lucide-react'

export function VisualStyleCard({ style, selected, disabled = false, onSelect, className = '' }) {
  return <button
    type="button"
    className={`ui-visual-style-card${selected ? ' is-selected' : ''}${className ? ` ${className}` : ''}`}
    aria-pressed={selected}
    disabled={disabled}
    onClick={() => onSelect?.(style.value)}
  >
    <img src={style.image} alt="" />
    <span><strong>{style.label}</strong><small>{style.description}</small></span>
    {selected ? <i aria-hidden="true"><CheckCircle2 size={15} /></i> : null}
  </button>
}
