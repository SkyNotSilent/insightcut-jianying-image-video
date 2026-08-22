import { RefreshCw, RotateCcw } from 'lucide-react'
import { getErrorPresentation } from '../../lib/errorMessages'
import { AssetStatus } from './assetState'
import './asset-components.css'

function primaryAction(status, callbacks) {
  if (status === 'failed' && callbacks.onRetry) {
    return { label: '重试配音', Icon: RotateCcw, onClick: callbacks.onRetry }
  }
  if (status === 'stale' && (callbacks.onUpdate || callbacks.onRegenerate)) {
    return { label: '更新配音', Icon: RefreshCw, onClick: callbacks.onUpdate || callbacks.onRegenerate }
  }
  if (status === 'complete' && callbacks.onRegenerate) {
    return { label: '重新生成', Icon: RefreshCw, onClick: callbacks.onRegenerate }
  }
  return null
}

/**
 * Audio sibling of ImageCard. It presents state and playback but performs no API work.
 */
export function AudioCard({
  status = 'waiting',
  src = '',
  title = '分镜配音',
  eyebrow,
  voiceLabel,
  duration,
  transcript,
  error,
  onPlay,
  onRetry,
  onRegenerate,
  onUpdate,
  actions,
  className = '',
}) {
  const presentation = status === 'failed' ? getErrorPresentation(error) : null
  const action = primaryAction(status, { onRetry, onRegenerate, onUpdate })
  const ActionIcon = action?.Icon

  return (
    <article className={`asset-card audio-asset-card is-${status}${className ? ` ${className}` : ''}`}>
      <div className="asset-card-copy">
        <div className="asset-card-heading">
          <div>
            {eyebrow ? <span className="asset-card-eyebrow">{eyebrow}</span> : null}
            <h3>{title}</h3>
          </div>
          <AssetStatus status={status} />
        </div>
        {(voiceLabel || duration) ? (
          <p className="asset-card-meta">{[voiceLabel, duration].filter(Boolean).join(' · ')}</p>
        ) : null}
        {transcript ? <p className="audio-asset-transcript">{transcript}</p> : null}
        {src ? <audio className="audio-asset-player" controls preload="none" src={src} onPlay={onPlay}>当前浏览器不支持音频播放。</audio> : null}
        {presentation ? (
          <div className="asset-inline-error" role="alert">
            <strong>{presentation.title}</strong>
            <span>{presentation.action}</span>
          </div>
        ) : null}
        {(action || actions) ? (
          <div className="audio-asset-actions" aria-label={`${title}操作`}>
            {action ? (
              <button type="button" className="asset-text-action" onClick={action.onClick}>
                <ActionIcon size={14} aria-hidden="true" />
                {action.label}
              </button>
            ) : null}
            {actions}
          </div>
        ) : null}
      </div>
    </article>
  )
}
