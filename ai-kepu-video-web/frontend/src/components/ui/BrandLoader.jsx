import './BrandLoader.css'

export function BrandLoader({ label = '正在载入' }) {
  return <div className="ui-brand-loader" role="status" aria-live="polite" aria-label={label}>
    <span className="ui-brand-loader-mark" aria-hidden="true">
      <span className="ui-brand-loader-glow" />
      <span className="ui-brand-loader-inner" />
      <span className="ui-brand-loader-reticles">
        <span className="ui-brand-loader-row">
          <i className="ui-brand-loader-corner ui-brand-loader-corner-tl" />
          <i className="ui-brand-loader-corner ui-brand-loader-corner-tr" />
        </span>
        <span className="ui-brand-loader-row">
          <i className="ui-brand-loader-corner ui-brand-loader-corner-bl" />
          <i className="ui-brand-loader-corner ui-brand-loader-corner-br" />
        </span>
      </span>
      <span className="ui-brand-loader-scan" />
      <span className="ui-brand-loader-dot" />
    </span>
  </div>
}
