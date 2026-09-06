import { useLayoutEffect, useId, useRef } from 'react'
import { X } from 'lucide-react'

export function Modal({ open, title, children, onClose, footer }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(onClose)
  const titleId = useId()

  closeRef.current = onClose

  useLayoutEffect(() => {
    if (!open) return undefined
    const previouslyFocused = document.activeElement
    const dialog = dialogRef.current
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusable = () => Array.from(dialog?.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) || []).filter(element => !element.hasAttribute('hidden') && element.getAttribute('aria-hidden') !== 'true')

    const initialTarget = dialog?.querySelector('[data-modal-initial-focus]') || focusable()[0] || dialog
    initialTarget?.focus?.()

    const onKeyDown = event => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current?.()
        return
      }
      if (event.key !== 'Tab') return
      const controls = focusable()
      if (!controls.length) {
        event.preventDefault()
        dialog?.focus()
        return
      }
      const first = controls[0]
      const last = controls[controls.length - 1]
      if (event.shiftKey && (document.activeElement === first || !dialog?.contains(document.activeElement))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    dialog?.addEventListener('keydown', onKeyDown)
    return () => {
      dialog?.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
      if (previouslyFocused instanceof HTMLElement && previouslyFocused.isConnected) previouslyFocused.focus()
    }
  }, [open])

  if (!open) return null

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => closeRef.current?.()}>
      <section ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex="-1" onMouseDown={event => event.stopPropagation()}>
        <header className="modal-header">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="icon-button" onClick={() => closeRef.current?.()} aria-label="关闭对话框" title="关闭对话框"><X size={18} aria-hidden="true" /></button>
        </header>
        <div className="modal-content">{children}</div>
        {footer && <footer className="modal-footer">{footer}</footer>}
      </section>
    </div>
  )
}

export function ConfirmDialog({ open, title = '确认操作', message, confirmLabel = '确认', cancelLabel = '取消', onConfirm, onClose, danger = false, confirmDisabled = false }) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      footer={<><button type="button" className="button button-secondary" data-modal-initial-focus onClick={onClose} disabled={confirmDisabled}>{cancelLabel}</button><button type="button" className={`button ${danger ? 'button-danger' : 'button-primary'}`} onClick={onConfirm} disabled={confirmDisabled}>{confirmLabel}</button></>}
    >
      <p>{message}</p>
    </Modal>
  )
}
