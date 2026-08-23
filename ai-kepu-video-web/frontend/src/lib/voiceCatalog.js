const SPEED_LEVEL_ORDER = ['very_slow', 'slow', 'normal', 'fast', 'very_fast']
const SPEED_LEVELS = new Set(SPEED_LEVEL_ORDER)
export const SPEED_LEVEL_OPTIONS = [
  ['very_slow', '很慢'],
  ['slow', '偏慢'],
  ['normal', '正常'],
  ['fast', '偏快'],
  ['very_fast', '很快'],
]
const DOUBAO_SPEED_RATIOS = {
  very_slow: 0.8,
  slow: 0.9,
  normal: 1,
  fast: 1.25,
  very_fast: 1.5,
}

export function doubaoSpeedRatio(speedLevel) {
  return DOUBAO_SPEED_RATIOS[SPEED_LEVELS.has(speedLevel) ? speedLevel : 'normal']
}

export function speedLevelPosition(speedLevel) {
  const position = SPEED_LEVEL_ORDER.indexOf(speedLevel)
  return position >= 0 ? position : SPEED_LEVEL_ORDER.indexOf('normal')
}

export function speedLevelAtPosition(position) {
  const parsed = Number.parseInt(position, 10)
  const clamped = Number.isFinite(parsed)
    ? Math.min(SPEED_LEVEL_ORDER.length - 1, Math.max(0, parsed))
    : SPEED_LEVEL_ORDER.indexOf('normal')
  return SPEED_LEVEL_ORDER[clamped]
}

function canonicalVoiceId(voice = {}) {
  const current = String(voice.id || '').trim()
  if (current.startsWith('mimo:') || current.startsWith('doubao:') || current.startsWith('mimo-clone:')) {
    return current
  }
  const provider = voice.provider === 'doubao' ? 'doubao' : 'mimo'
  const voiceId = String(voice.voice_id || current).trim()
  if (!voiceId) return ''
  return voice.kind === 'clone' ? `mimo-clone:${voiceId}` : `${provider}:${voiceId}`
}

export function normalizeVoiceCatalog(input) {
  if (!Array.isArray(input)) return []
  return input.map((voice, index) => {
    const provider = voice?.provider === 'doubao' ? 'doubao' : 'mimo'
    const kind = voice?.kind === 'clone' ? 'clone' : 'preset'
    const id = canonicalVoiceId({ ...voice, provider, kind })
    const status = voice?.status || 'ready'
    const isEnabled = Boolean(voice?.is_enabled)
    return {
      ...(voice || {}),
      id,
      voice_id: String(voice?.voice_id || id.split(':').slice(1).join(':') || ''),
      name: String(voice?.name || voice?.voice_id || `音色 ${index + 1}`),
      provider,
      kind,
      status,
      is_enabled: isEnabled,
      isClone: kind === 'clone',
      selectable: status === 'ready' && (kind === 'preset' || isEnabled),
      preview_url: voice?.preview_url || '',
    }
  }).filter(voice => voice.id)
}

export function groupVisibleVoices(voices, { includeUnavailable = false } = {}) {
  const source = normalizeVoiceCatalog(voices)
  return ['mimo', 'doubao'].map(provider => ({
    provider,
    label: provider === 'mimo' ? '小米 MiMo' : '豆包 TTS',
    voices: source.filter(voice => (
      voice.provider === provider && (includeUnavailable || voice.selectable)
    )),
  })).filter(group => group.voices.length)
}

export function resolveEnabledVoiceDefaults(input, defaults = {}, requestedProvider = 'doubao') {
  const enabled = normalizeVoiceCatalog(input).filter(voice => voice.selectable)
  const byProvider = {
    doubao: enabled.filter(voice => voice.provider === 'doubao'),
    mimo: enabled.filter(voice => voice.provider === 'mimo'),
  }
  const canonicalDefault = (provider, value) => {
    const raw = String(value || '').trim()
    if (!raw) return ''
    if (raw.startsWith('mimo-clone:') || raw.startsWith(`${provider}:`)) return raw
    return `${provider}:${raw}`
  }
  const nextDefaults = Object.fromEntries(['doubao', 'mimo'].map(provider => {
    const current = canonicalDefault(provider, defaults[provider])
    const next = byProvider[provider].some(voice => voice.id === current)
      ? current
      : byProvider[provider][0]?.id || ''
    return [provider, next]
  }))
  const availableProviders = ['doubao', 'mimo'].filter(provider => byProvider[provider].length)
  return {
    provider: availableProviders.includes(requestedProvider) ? requestedProvider : availableProviders[0] || '',
    availableProviders,
    defaults: nextDefaults,
  }
}

export function reconcileTtsVoiceConfig(tts = {}, input, activatedProvider = '') {
  const resolved = resolveEnabledVoiceDefaults(input, {
    doubao: tts.default_voice,
    mimo: tts.mimo?.default_voice,
  }, tts.provider)
  let enabledProviders = (Array.isArray(tts.enabled_providers) ? tts.enabled_providers : [])
    .filter(provider => resolved.availableProviders.includes(provider))
  if (activatedProvider && resolved.availableProviders.includes(activatedProvider) && !enabledProviders.includes(activatedProvider)) {
    enabledProviders = [...enabledProviders, activatedProvider]
  }
  if (!enabledProviders.length && resolved.provider) enabledProviders = [resolved.provider]
  const provider = enabledProviders.includes(tts.provider)
    ? tts.provider
    : enabledProviders[0] || tts.provider || 'doubao'
  const mimoDefault = resolved.defaults.mimo.startsWith('mimo-clone:')
    ? resolved.defaults.mimo
    : resolved.defaults.mimo.replace(/^mimo:/, '')
  return {
    ...tts,
    provider,
    enabled_providers: enabledProviders,
    default_voice: resolved.defaults.doubao.replace(/^doubao:/, ''),
    mimo: { ...(tts.mimo || {}), default_voice: mimoDefault },
  }
}

export function hasUsableVoice(input, enabledProviders = []) {
  const providers = new Set(enabledProviders)
  return normalizeVoiceCatalog(input).some(voice => (
    voice.selectable && providers.has(voice.provider)
  ))
}

export function mergeTtsOptions(base = {}, override = {}, provider = 'mimo') {
  const merged = { ...(base || {}), ...(override || {}) }
  const speedLevel = SPEED_LEVELS.has(merged.speed_level) ? merged.speed_level : 'normal'
  if (provider === 'doubao') {
    const parsedVolume = Number(merged.volume_ratio)
    return {
      speed_level: speedLevel,
      volume_ratio: Number.isFinite(parsedVolume)
        ? Math.min(2, Math.max(0.5, parsedVolume))
        : 1,
    }
  }
  return {
    speed_level: speedLevel,
    style_prompt: String(merged.style_prompt || '').trim().slice(0, 300),
  }
}

export function buildVoiceTaskPayload(voiceType, options = {}, inherited = {}) {
  const id = canonicalVoiceId({
    id: voiceType,
    provider: String(voiceType || '').startsWith('doubao:') ? 'doubao' : 'mimo',
    kind: String(voiceType || '').startsWith('mimo-clone:') ? 'clone' : 'preset',
  })
  if (!id) return { voice_type: null, tts_options: {} }
  const provider = id.startsWith('doubao:') ? 'doubao' : 'mimo'
  return {
    voice_type: id,
    tts_options: mergeTtsOptions(inherited, options, provider),
  }
}

export function parseSegmentTtsOptions(segment = {}) {
  if (segment.audio_tts_options && typeof segment.audio_tts_options === 'object') {
    return { ...segment.audio_tts_options }
  }
  try {
    const parsed = JSON.parse(segment.audio_tts_options_json || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const { _segment_override: ignored, speed_ratio: ignoredRatio, speed_instruction: ignoredInstruction, ...options } = parsed
    return options
  } catch {
    return {}
  }
}

export function resolveSegmentVoiceSettings(segment = {}, workspace = {}) {
  const voiceType = segment.audio_voice_type || workspace.voice_type || ''
  const provider = voiceType.startsWith('doubao:') ? 'doubao' : 'mimo'
  const override = parseSegmentTtsOptions(segment)
  const effectiveOptions = mergeTtsOptions(workspace.tts_options || {}, override, provider)
  return {
    voiceType,
    provider,
    override,
    speedOverride: override.speed_level || '',
    effectiveOptions,
  }
}

export function nextPreviewState(state = {}, action = {}) {
  const current = {
    playingVoice: state.playingVoice || '',
    token: state.token || 0,
    loading: Boolean(state.loading),
    url: state.url || '',
    error: state.error || '',
  }
  if (action.type === 'stop') {
    return { playingVoice: '', token: current.token, loading: false, url: '', error: '' }
  }
  if (action.type === 'start') {
    return {
      playingVoice: action.voiceId || '',
      token: action.token || current.token + 1,
      loading: true,
      url: '',
      error: '',
    }
  }
  if (action.token !== current.token || action.voiceId !== current.playingVoice) return current
  if (action.type === 'ready') return { ...current, loading: false, url: action.url || '' }
  if (action.type === 'error') return { ...current, loading: false, url: '', error: action.error || '试听失败' }
  return current
}
