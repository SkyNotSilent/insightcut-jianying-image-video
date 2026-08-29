import { Check, History, RotateCcw } from 'lucide-react'
import { AssetStatus } from './assetState'
import './asset-components.css'

function sourceLabel(source) {
  return ({ generated: '初次生成', regenerated: '重新生成', upload: '上传替换', selected: '已回选', legacy: '历史素材' })[source] || '素材版本'
}

/**
 * Compact horizontal version rail. The parent owns selection and restoration.
 */
export function AssetHistory({
  versions = [],
  selectedId,
  title = '素材版本',
  emptyMessage = '还没有历史版本',
  onSelect,
  onRestore,
  renderPreview,
  className = '',
}) {
  return (
    <section className={`asset-history${className ? ` ${className}` : ''}`} aria-label={title}>
      <header className="asset-history-heading">
        <span aria-hidden="true"><History size={15} /></span>
        <h3>{title}</h3>
        <small>{versions.length} 个版本</small>
      </header>

      {versions.length ? (
        <ol className="asset-history-rail">
          {versions.map((version, index) => {
            const isSelected = version.id === selectedId
            const versionLabel = version.label || `版本 ${index + 1}`
            const versionTime = version.createdAt || version.description || '时间未知'
            return (
              <li key={version.id} className={isSelected ? 'is-selected' : ''}>
                <button
                  type="button"
                  className="asset-history-version"
                  aria-pressed={isSelected}
                  aria-label={`${versionLabel} ${versionTime}`}
                  onClick={() => onSelect?.(version, index)}
                >
                  <span className="asset-history-preview">
                    {renderPreview ? renderPreview(version, index) : version.thumbnail ? <img src={version.thumbnail} alt="" /> : <History size={20} aria-hidden="true" />}
                  </span>
                  <span className="asset-history-copy">
                    <strong>{versionLabel}</strong>
                    <small>{versionTime}</small>
                  </span>
                  {isSelected ? <Check className="asset-history-check" size={15} aria-hidden="true" /> : null}
                </button>
                <div className="asset-history-footer">
                  <span className="asset-history-source">{sourceLabel(version.source)}</span>
                  {onRestore && !isSelected && version.restorable !== false ? (
                    <button type="button" className="asset-history-restore" onClick={() => onRestore(version, index)} aria-label={`恢复${version.label || `版本 ${index + 1}`}`}>
                      <RotateCcw size={13} aria-hidden="true" />
                      恢复
                    </button>
                  ) : null}
                  {isSelected || version.restorable === false ? <AssetStatus status={version.status || 'complete'} /> : null}
                </div>
              </li>
            )
          })}
        </ol>
      ) : <p className="asset-history-empty">{emptyMessage}</p>}
    </section>
  )
}
