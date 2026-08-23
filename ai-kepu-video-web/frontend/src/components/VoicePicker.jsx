import { Check, CircleAlert, LoaderCircle, Pause, Play, Sparkles, Volume2 } from 'lucide-react'
import { SPEED_LEVEL_OPTIONS, doubaoSpeedRatio, groupVisibleVoices, mergeTtsOptions, normalizeVoiceCatalog, speedLevelAtPosition, speedLevelPosition } from '../lib/voiceCatalog'
import './voice-picker.css'

export function VoicePicker({
  voices = [],
  value = '',
  ttsOptions = {},
  onChange,
  onOptionsChange,
  onPreview,
  playingVoice = '',
  previewLoading = false,
  previewError = '',
  showAdvanced = true,
  includeUnavailable = false,
  optionsProvider = '',
  compact = false,
  disabled = false,
}) {
  const normalized = normalizeVoiceCatalog(voices)
  const groups = groupVisibleVoices(normalized, { includeUnavailable })
  const selected = normalized.find(voice => voice.id === value)
  const provider = selected?.provider || optionsProvider || (String(value).startsWith('doubao:') ? 'doubao' : 'mimo')
  const options = mergeTtsOptions({}, ttsOptions, provider)
  const speedRatio = provider === 'doubao' ? doubaoSpeedRatio(options.speed_level) : null
  const speedPosition = speedLevelPosition(options.speed_level)

  const updateOptions = patch => {
    onOptionsChange?.(mergeTtsOptions(options, patch, provider))
  }

  if (!groups.length) {
    return <div className="voice-picker-empty"><Volume2 size={18} /><span>当前没有可用音色</span></div>
  }

  return <div className={`voice-picker${compact ? ' is-compact' : ''}`}>
    <div className="voice-picker-groups">
      {groups.map(group => <section className="voice-provider-group" key={group.provider}>
        <header><span>{group.label}</span><small>{group.voices.length} 个音色</small></header>
        <div className="voice-card-grid">
          {group.voices.map(voice => {
            const selectedVoice = voice.id === value
            const checkedVoice = selectedVoice
            const previewing = voice.id === playingVoice
            const cardSelectable = voice.selectable
            return <article className={`voice-card${checkedVoice ? ' is-selected' : ''}${cardSelectable ? '' : ' is-unavailable'}`} key={voice.id}>
              <button
                type="button"
                className="voice-card-select"
                disabled={disabled || !cardSelectable}
                aria-pressed={checkedVoice}
                onClick={() => onChange?.(voice.id, voice)}
              >
                <span className="voice-avatar" aria-hidden="true">{voice.kind === 'clone' ? <Sparkles size={16} /> : voice.name.slice(0, 1)}</span>
                <span className="voice-card-copy"><strong>{voice.name}</strong><small>{voice.kind === 'clone' ? '克隆音色' : voice.description || (voice.gender === 'male' ? '男声' : voice.gender === 'female' ? '女声' : '预置音色')}</small></span>
                {checkedVoice ? <span className="voice-selected-mark"><Check size={13} /></span> : null}
              </button>
              <button
                type="button"
                className="voice-preview-button"
                disabled={previewLoading && !previewing}
                aria-label={`${previewing ? '停止' : '试听'}${voice.name}`}
                onClick={() => onPreview?.(voice)}
              >
                {previewing && previewLoading ? <LoaderCircle className="spin" size={15} /> : previewing ? <Pause size={15} /> : <Play size={15} />}
                <span>{previewing ? '停止' : '试听'}</span>
              </button>
              {!cardSelectable ? <span className="voice-status-label">{voice.status === 'draft' ? '待试听' : voice.status === 'failed' ? '试听失败' : '不可用'}</span> : null}
            </article>
          })}
        </div>
      </section>)}
    </div>

    {previewError ? <p className="voice-preview-error" role="alert"><CircleAlert size={15} />{previewError}</p> : null}

    {showAdvanced && selected ? <section className="voice-advanced" aria-label="配音参数">
      <label className={`voice-speed-control${provider === 'doubao' ? ' is-wide' : ''}`}><span>语速{speedRatio ? <strong>{speedRatio}x</strong> : null}</span><select aria-label="语速档位" value={options.speed_level} disabled={disabled} onChange={event => updateOptions({ speed_level: event.target.value })}>{SPEED_LEVEL_OPTIONS.map(([key, label]) => <option value={key} key={key}>{provider === 'doubao' ? `${label} · ${doubaoSpeedRatio(key)}x` : label}</option>)}</select><input aria-label="语速档位滑杆" aria-valuetext={SPEED_LEVEL_OPTIONS[speedPosition][1]} type="range" min="0" max="4" step="1" value={speedPosition} disabled={disabled} onChange={event => updateOptions({ speed_level: speedLevelAtPosition(event.target.value) })} /><span className="voice-speed-ticks" aria-hidden="true">{SPEED_LEVEL_OPTIONS.map(([key, label]) => <small className={key === options.speed_level ? 'is-active' : ''} key={key}>{label}</small>)}</span><small className="voice-speed-hint">下拉档位与滑杆同步，试听和最终配音使用同一语速</small></label>
      {provider === 'mimo' ? <label className="voice-style-field"><span>风格指令</span><input value={options.style_prompt} maxLength="300" disabled={disabled} placeholder="例如：轻松、有感情，适合短视频旁白" onChange={event => updateOptions({ style_prompt: event.target.value })} /></label> : null}
    </section> : null}
  </div>
}
