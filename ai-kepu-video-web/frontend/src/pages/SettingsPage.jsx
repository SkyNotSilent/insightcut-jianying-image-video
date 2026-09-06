import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  CheckCircle2,
  CircleAlert,
  Cpu,
  Image,
  LoaderCircle,
  Mic,
  MessageSquareText,
  Pencil,
  RefreshCw,
  Save,
  Square,
  Trash2,
  Upload,
  Volume2,
} from 'lucide-react'
import { useNavigate } from 'react-router'
import {
  createVoiceClone,
  deleteVoiceClone,
  getConfig,
  getLlmProviderModels,
  getLlmProviders,
  getVoiceClones,
  getVoices,
  previewVoice,
  previewVoiceClone,
  replaceVoiceCloneReference,
  refreshLlmProviderModels,
  testTtsConfig,
  updateConfig,
  updateVoiceClone,
} from '../api/task'
import { AdvancedSettings } from '../components/AdvancedSettings'
import { ConfirmDialog } from '../components/Modal'
import { LlmProviderSettings } from '../components/LlmProviderSettings'
import { VoicePicker } from '../components/VoicePicker'
import { EmptyState, LoadingState } from '../components/StatusStates'
import { EmptyStateCard } from '../components/ui/EmptyStateCard'
import {
  AGNES_PRESET,
  normalizeConcurrency,
  normalizeConfig,
  normalizeRetryCount,
  normalizeRetryInterval,
  restoreAgnesPreset,
  restoreMimoTechnicalPreset,
  validateConfig,
  validateTtsTest,
} from '../lib/settingsConfig'
import {
  buildProviderRefreshPayload,
  chooseProviderModel,
  fallbackProviders,
  isCurrentProviderRequest,
  isLlmProviderReady,
  mergeProviderModels,
  normalizeProviders,
  switchProviderDraft,
} from '../lib/llmProviderCatalog'
import { toast } from '../lib/toast'
import { hasUsableVoice, nextPreviewState, normalizeVoiceCatalog, reconcileTtsVoiceConfig } from '../lib/voiceCatalog'
import { normalizeMediaUrl } from '../utils/mediaUrl'
import './delivery-pages.css'

const IMAGE_SIZES = ['auto', '1024x1024', '1536x1024', '1024x1536', '1792x1024', '1024x1792', '1920x1080', '1080x1920']
const SECTION_LINKS = [
  { id: 'settings-llm', label: '生文模型', icon: MessageSquareText },
  { id: 'settings-image', label: 'Agnes 生图', icon: Image },
  { id: 'settings-tts', label: '配音模型', icon: Volume2 },
  { id: 'settings-runtime', label: '生成策略', icon: Cpu },
]

export function SettingsPage({ embedded = false, onClose }) {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [saving, setSaving] = useState(false)
  const [testingTts, setTestingTts] = useState(false)
  const [ttsTestUrl, setTtsTestUrl] = useState('')
  const configRevisionRef = useRef(null)
  const [form, setForm] = useState(() => normalizeConfig())
  const [llmProviders, setLlmProviders] = useState([])
  const [localModels, setLocalModels] = useState([])
  const [accountModels, setAccountModels] = useState([])
  const [llmSyncState, setLlmSyncState] = useState({ status: 'idle', message: '' })
  const [providerTab, setProviderTab] = useState('mimo')
  const [voices, setVoices] = useState([])
  const [clones, setClones] = useState([])
  const [previewState, setPreviewState] = useState(() => nextPreviewState())
  const [cloneName, setCloneName] = useState('')
  const [cloneConsent, setCloneConsent] = useState(false)
  const [cloneFile, setCloneFile] = useState(null)
  const [cloneBusy, setCloneBusy] = useState('')
  const [focusedClone, setFocusedClone] = useState('')
  useEffect(() => {
    if (focusedClone) document.getElementById(`clone-${focusedClone}`)?.focus()
  }, [focusedClone])
  const [cloneToDelete, setCloneToDelete] = useState(null)
  const [recording, setRecording] = useState(false)
  const audioRef = useRef(null)
  const previewTokenRef = useRef(0)
  const recorderRef = useRef(null)
  const recordingChunksRef = useRef([])
  const llmDraftsRef = useRef({})
  const loadGenerationRef = useRef(0)
  const localModelsRequestRef = useRef(0)
  const syncRequestRef = useRef(0)
  const currentLlmProviderRef = useRef(form.llm.provider)

  const loadConfig = useCallback(async () => {
    const loadGeneration = ++loadGenerationRef.current
    const localRequestId = ++localModelsRequestRef.current
    syncRequestRef.current += 1
    setLoading(true)
    setTtsTestUrl('')
    setLoadError('')
    try {
      const [config, catalog, cloneList, providerResult] = await Promise.all([
        getConfig(),
        getVoices({ include_disabled: true }),
        getVoiceClones({ include_hidden: true }),
        getLlmProviders()
          .then(data => ({ data, error: null }))
          .catch(error => ({ data: null, error })),
      ])
      if (loadGeneration !== loadGenerationRef.current) return
      configRevisionRef.current = config.revision
      const normalizedVoices = normalizeVoiceCatalog(catalog)
      const normalizedConfig = normalizeConfig(config)
      const normalized = {
        ...normalizedConfig,
        tts: reconcileTtsVoiceConfig(normalizedConfig.tts, normalizedVoices),
      }
      let providers = normalizeProviders(providerResult.data)
      if (!providers.length) {
        if (providerResult.error) console.warn('生文服务商目录加载失败，使用当前配置回退', providerResult.error)
        providers = fallbackProviders(normalized.llm)
      } else if (!providers.some(provider => provider.id === normalized.llm.provider)) {
        const synthetic = fallbackProviders(normalized.llm)
        const knownIds = new Set(providers.map(provider => provider.id))
        providers = [...synthetic.filter(provider => !knownIds.has(provider.id)), ...providers]
      }

      const currentProvider = providers.find(provider => provider.id === normalized.llm.provider)
        || providers[0]
      const providerId = currentProvider?.id || normalized.llm.provider
      currentLlmProviderRef.current = providerId
      const initialRequest = { loadGeneration, requestId: localRequestId, providerId }
      let initialModels = []
      if (currentProvider?.id) {
        try {
          const result = await getLlmProviderModels(currentProvider.id)
          initialModels = Array.isArray(result?.models) ? result.models : []
        } catch (error) {
          if (isCurrentProviderRequest(initialRequest, {
            loadGeneration: loadGenerationRef.current,
            requestId: localModelsRequestRef.current,
            providerId: currentLlmProviderRef.current,
          })) console.warn('本地生文模型列表加载失败', error)
        }
      }
      if (!isCurrentProviderRequest(initialRequest, {
        loadGeneration: loadGenerationRef.current,
        requestId: localModelsRequestRef.current,
        providerId: currentLlmProviderRef.current,
      })) return
      const initialModel = chooseProviderModel(normalized.llm.model, currentProvider, initialModels)
      const initialized = initialModel === normalized.llm.model
        ? normalized
        : { ...normalized, llm: { ...normalized.llm, model: initialModel } }

      setForm(initialized)
      setLlmProviders(providers)
      setLocalModels(initialModels)
      setAccountModels([])
      setLlmSyncState({ status: 'idle', message: '' })
      llmDraftsRef.current = initialized.llm.provider
        ? { [initialized.llm.provider]: { ...initialized.llm, provider_options: { ...(initialized.llm.provider_options || {}) } } }
        : {}
      setProviderTab(normalized.tts.provider)
      setVoices(normalizedVoices)
      setClones(Array.isArray(cloneList) ? cloneList : [])
    } catch (error) {
      if (loadGeneration !== loadGenerationRef.current) return
      console.error('加载 API 配置失败', error)
      setLoadError('API 配置暂不可用，请确认后端服务在线后重试。')
      toast.error('加载 API 配置失败')
    } finally {
      if (loadGeneration === loadGenerationRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => { loadConfig() }, [loadConfig])

  useEffect(() => () => {
    loadGenerationRef.current += 1
    localModelsRequestRef.current += 1
    syncRequestRef.current += 1
    audioRef.current?.pause()
    recorderRef.current?.stream?.getTracks?.().forEach(track => track.stop())
  }, [])

  const selectedLlmProvider = useMemo(
    () => llmProviders.find(provider => provider.id === form.llm.provider) || null,
    [form.llm.provider, llmProviders],
  )
  const displayedLlmModels = useMemo(
    () => mergeProviderModels(
      localModels,
      accountModels,
      form.llm.model,
      selectedLlmProvider?.recommended_model,
    ),
    [accountModels, form.llm.model, localModels, selectedLlmProvider?.recommended_model],
  )

  const readiness = useMemo(() => {
    const llmReady = Boolean(
      selectedLlmProvider
      && isLlmProviderReady(form.llm, selectedLlmProvider)
      && form.llm.model?.trim(),
    )
    const imageReady = Boolean(form.image.api_url && form.image.api_key && form.image.model)
    const doubaoEnabled = form.tts.enabled_providers.includes('doubao')
    const mimoEnabled = form.tts.enabled_providers.includes('mimo')
    const doubaoReady = !doubaoEnabled || (form.tts.auth_method === 'api_key'
      ? Boolean(form.tts.api_url && form.tts.api_key && form.tts.cluster && form.tts.default_voice)
      : Boolean(form.tts.api_url && form.tts.appid && form.tts.token && form.tts.cluster && form.tts.default_voice))
    const mimoReady = !mimoEnabled || Boolean(form.tts.mimo.base_url && form.tts.mimo.api_key && form.tts.mimo.model && form.tts.mimo.clone_model && form.tts.mimo.default_voice)
    return [
      { label: '生文 API', ready: llmReady, detail: llmReady ? form.llm.model : '配置不完整' },
      { label: 'Agnes 生图', ready: imageReady, detail: imageReady ? form.image.model : '配置不完整' },
      { label: '豆包 TTS', ready: doubaoReady, detail: doubaoEnabled ? (doubaoReady ? '已启用' : '配置不完整') : '未启用' },
      { label: 'MiMo TTS', ready: mimoReady, detail: mimoEnabled ? (mimoReady ? '已启用' : '配置不完整') : '未启用' },
    ]
  }, [form, selectedLlmProvider])

  const updateSection = (section, key, value) => setForm(current => ({ ...current, [section]: { ...current[section], [key]: value } }))
  const updateTts = (key, value) => setForm(current => ({ ...current, tts: { ...current.tts, [key]: value } }))
  const updateMimo = (key, value) => setForm(current => ({ ...current, tts: { ...current.tts, mimo: { ...current.tts.mimo, [key]: value } } }))

  const loadLlmProviderModels = useCallback(async (
    provider,
    { clear = true, initialization = {} } = {},
  ) => {
    const loadGeneration = loadGenerationRef.current
    const requestId = ++localModelsRequestRef.current
    const request = { loadGeneration, requestId, providerId: provider.id }
    if (clear) setLocalModels([])
    try {
      const result = await getLlmProviderModels(provider.id)
      if (!isCurrentProviderRequest(request, {
        loadGeneration: loadGenerationRef.current,
        requestId: localModelsRequestRef.current,
        providerId: currentLlmProviderRef.current,
      })) return
      const models = Array.isArray(result?.models) ? result.models : []
      setLocalModels(models)
      setForm(current => {
        if (current.llm.provider !== provider.id) return current
        const model = chooseProviderModel(current.llm.model, provider, models, initialization)
        return model === current.llm.model
          ? current
          : { ...current, llm: { ...current.llm, model } }
      })
    } catch (error) {
      if (!isCurrentProviderRequest(request, {
        loadGeneration: loadGenerationRef.current,
        requestId: localModelsRequestRef.current,
        providerId: currentLlmProviderRef.current,
      })) return
      console.warn(`加载 ${provider.id} 本地模型列表失败`, error)
    }
  }, [])

  const updateLlm = nextLlm => {
    currentLlmProviderRef.current = nextLlm.provider
    setForm(current => ({ ...current, llm: nextLlm }))
  }

  const selectLlmProvider = provider => {
    if (!provider || provider.id === form.llm.provider) return
    const switched = switchProviderDraft(llmDraftsRef.current, form.llm, provider)
    llmDraftsRef.current = switched.drafts
    currentLlmProviderRef.current = provider.id
    syncRequestRef.current += 1
    setAccountModels([])
    setLlmSyncState({ status: 'idle', message: '' })
    setForm(current => ({ ...current, llm: switched.llm }))
    void loadLlmProviderModels(provider, {
      initialization: {
        firstUse: switched.firstUse,
        presetModel: switched.presetModel,
      },
    })
  }

  const syncLlmProviderModels = async () => {
    if (!selectedLlmProvider) return
    const provider = selectedLlmProvider
    const loadGeneration = loadGenerationRef.current
    const requestId = ++syncRequestRef.current
    const request = { loadGeneration, requestId, providerId: provider.id }
    setLlmSyncState({ status: 'loading', message: '' })
    try {
      const result = await refreshLlmProviderModels(
        provider.id,
        buildProviderRefreshPayload(form.llm, provider),
      )
      if (!isCurrentProviderRequest(request, {
        loadGeneration: loadGenerationRef.current,
        requestId: syncRequestRef.current,
        providerId: currentLlmProviderRef.current,
      })) return
      const refreshed = Array.isArray(result?.models) ? result.models : []
      const availableToAccount = refreshed.filter(model => model?.sources?.includes('account'))
      const syncedModels = availableToAccount.length
        ? availableToAccount
        : refreshed.map(model => ({
          ...model,
          sources: [...new Set([...(model?.sources || []), 'account'])],
        }))
      setAccountModels(syncedModels)
      setForm(current => {
        if (current.llm.provider !== provider.id) return current
        const model = chooseProviderModel(current.llm.model, provider, [...localModels, ...syncedModels])
        return model === current.llm.model
          ? current
          : { ...current, llm: { ...current.llm, model } }
      })
      setLlmSyncState({
        status: 'success',
        message: `验证成功，已同步 ${syncedModels.length} 个当前账号模型。`,
      })
    } catch (error) {
      if (!isCurrentProviderRequest(request, {
        loadGeneration: loadGenerationRef.current,
        requestId: syncRequestRef.current,
        providerId: currentLlmProviderRef.current,
      })) return
      setLlmSyncState({
        status: 'error',
        message: error?.response?.data?.detail || '验证或同步失败，已保留当前模型列表和选择。',
      })
    }
  }

  const refreshVoiceData = useCallback(async () => {
    const [catalog, cloneList] = await Promise.all([
      getVoices({ include_disabled: true }),
      getVoiceClones({ include_hidden: true }),
    ])
    const normalizedVoices = normalizeVoiceCatalog(catalog)
    setVoices(normalizedVoices)
    setForm(current => ({
      ...current,
      tts: reconcileTtsVoiceConfig(current.tts, normalizedVoices),
    }))
    setClones(Array.isArray(cloneList) ? cloneList : [])
  }, [])

  const setProviderEnabled = (provider, enabled) => {
    const providerHasVoice = voices.some(voice => (
      voice.provider === provider && voice.selectable
    ))
    if (enabled && !providerHasVoice) {
      toast.warning('该服务商当前没有可用音色')
      return
    }
    const existing = form.tts.enabled_providers || []
    let next = enabled
      ? [...new Set([...existing, provider])]
      : existing.filter(item => item !== provider)
    if (!next.length) {
      const fallback = ['doubao', 'mimo'].find(item => (
        item !== provider && voices.some(voice => voice.provider === item && voice.selectable)
      ))
      if (!fallback) {
        toast.warning('至少保留一个含可用音色的 TTS 服务商')
        return
      }
      next = [fallback]
    }
    setForm(current => {
      const tts = reconcileTtsVoiceConfig({ ...current.tts, enabled_providers: next }, voices)
      return { ...current, tts }
    })
  }

  const defaultVoiceKey = providerTab === 'mimo'
    ? String(form.tts.mimo.default_voice || '').startsWith('mimo-clone:')
      ? form.tts.mimo.default_voice
      : `mimo:${form.tts.mimo.default_voice}`
    : String(form.tts.default_voice || '').startsWith('doubao:')
      ? form.tts.default_voice
      : `doubao:${form.tts.default_voice}`

  const providerVoices = voices.filter(voice => voice.provider === providerTab && voice.kind === 'preset')
  const providerOptions = providerTab === 'mimo'
    ? { speed_level: form.tts.mimo.speed_level, style_prompt: form.tts.mimo.style_prompt }
    : { speed_level: form.tts.speed_level, volume_ratio: form.tts.volume_ratio }

  const selectDefaultVoice = voiceId => {
    if (providerTab === 'doubao') {
      updateTts('default_voice', voiceId.replace(/^doubao:/, ''))
    } else {
      updateMimo('default_voice', voiceId.startsWith('mimo-clone:') ? voiceId : voiceId.replace(/^mimo:/, ''))
    }
  }

  const updateProviderOptions = options => {
    if (providerTab === 'doubao') {
      setForm(current => ({ ...current, tts: { ...current.tts, speed_level: options.speed_level, volume_ratio: options.volume_ratio } }))
    } else {
      setForm(current => ({ ...current, tts: { ...current.tts, mimo: { ...current.tts.mimo, speed_level: options.speed_level, style_prompt: options.style_prompt } } }))
    }
  }

  const stopPreview = useCallback(() => {
    audioRef.current?.pause()
    audioRef.current = null
    setPreviewState(current => nextPreviewState(current, { type: 'stop' }))
  }, [])

  const playPreviewUrl = useCallback((voiceId, token, url) => {
    const audio = new Audio(normalizeMediaUrl(url))
    audioRef.current = audio
    audio.onended = stopPreview
    audio.onerror = () => setPreviewState(current => nextPreviewState(current, { type: 'error', voiceId, token, error: '试听音频播放失败' }))
    setPreviewState(current => nextPreviewState(current, { type: 'ready', voiceId, token, url }))
    audio.play().catch(() => setPreviewState(current => nextPreviewState(current, { type: 'error', voiceId, token, error: '浏览器未能开始播放' })))
  }, [stopPreview])

  const handleVoicePreview = async voice => {
    if (previewState.playingVoice === voice.id) {
      stopPreview()
      return
    }
    stopPreview()
    const token = ++previewTokenRef.current
    setPreviewState(current => nextPreviewState(current, { type: 'start', voiceId: voice.id, token }))
    try {
      const result = await previewVoice({
        voice_type: voice.id,
        tts_options: providerOptions,
        config_override: form.tts,
      })
      playPreviewUrl(voice.id, token, result.url)
    } catch (error) {
      setPreviewState(current => nextPreviewState(current, { type: 'error', voiceId: voice.id, token, error: error?.response?.data?.detail || '音色试听失败' }))
    }
  }

  const createClone = async () => {
    if (!cloneName.trim()) return toast.warning('请填写克隆音色名称')
    if (!cloneFile) return toast.warning('请选择或录制参考音频')
    if (!cloneConsent) return toast.warning('请确认已获得该声音的使用授权')
    setCloneBusy('create')
    try {
      const created = await createVoiceClone({ name: cloneName.trim(), consentConfirmed: true, file: cloneFile })
      try {
        await previewVoiceClone(created.clone_id, { text: form.tts.preview_text, tts_options: providerOptions, config_override: form.tts })
        toast.success('克隆音色已创建并通过试听')
      } catch (error) {
        toast.warning('参考音频已保存，但试听失败，可稍后重试')
      }
      setCloneName('')
      setCloneFile(null)
      setCloneConsent(false)
      await refreshVoiceData()
    } catch (error) {
      toast.error(error?.response?.data?.detail || '创建克隆音色失败')
    } finally {
      setCloneBusy('')
    }
  }

  const startRecording = async () => {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return toast.warning('当前浏览器不支持录音')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const recorder = new MediaRecorder(stream)
      recordingChunksRef.current = []
      recorder.ondataavailable = event => { if (event.data?.size) recordingChunksRef.current.push(event.data) }
      recorder.onstop = () => {
        const type = recorder.mimeType || 'audio/webm'
        const blob = new Blob(recordingChunksRef.current, { type })
        setCloneFile(new File([blob], `recording-${Date.now()}.webm`, { type }))
        stream.getTracks().forEach(track => track.stop())
        setRecording(false)
      }
      recorderRef.current = recorder
      recorder.start()
      setRecording(true)
    } catch (error) {
      toast.error('无法访问麦克风，请检查浏览器权限')
    }
  }

  const retryClonePreview = async clone => {
    setCloneBusy(`preview:${clone.clone_id}`)
    try {
      const result = await previewVoiceClone(clone.clone_id, { text: form.tts.preview_text, tts_options: providerOptions, config_override: form.tts })
      await refreshVoiceData()
      const token = ++previewTokenRef.current
      const voiceId = `mimo-clone:${clone.clone_id}`
      setPreviewState(current => nextPreviewState(current, { type: 'start', voiceId, token }))
      playPreviewUrl(voiceId, token, result.clone.preview_url || result.preview.url)
    } catch (error) {
      toast.error(error?.response?.data?.detail || '克隆试听失败')
      await refreshVoiceData()
    } finally {
      setCloneBusy('')
    }
  }

  const patchClone = async (cloneId, patch) => {
    setCloneBusy(`patch:${cloneId}`)
    try {
      await updateVoiceClone(cloneId, patch)
      await refreshVoiceData()
    } catch (error) {
      toast.error(error?.response?.data?.detail || '更新克隆音色失败')
    } finally {
      setCloneBusy('')
    }
  }

  const replaceClone = async (cloneId, file) => {
    if (!file) return
    setCloneBusy(`replace:${cloneId}`)
    try {
      const updated = await replaceVoiceCloneReference(cloneId, file)
      setFocusedClone('')
      toast.success(updated.clone_id !== cloneId ? '已创建新录音音色，旧任务继续使用原音色；请生成试听' : '参考音频已替换，请重新生成试听')
      await refreshVoiceData()
      setFocusedClone(updated.clone_id)
    } catch (error) {
      toast.error(error?.response?.data?.detail || '替换参考音频失败')
    } finally {
      setCloneBusy('')
    }
  }

  const removeClone = async () => {
    const cloneId = cloneToDelete?.clone_id
    if (!cloneId || cloneBusy === `delete:${cloneId}`) return
    setCloneBusy(`delete:${cloneId}`)
    try {
      await deleteVoiceClone(cloneId)
      await refreshVoiceData()
      setCloneToDelete(null)
      toast.success('克隆音色已处理')
    } catch (error) {
      toast.error(error?.response?.data?.detail || '删除克隆音色失败')
    } finally {
      setCloneBusy('')
    }
  }

  const saveConfig = async () => {
    const reconciledTts = reconcileTtsVoiceConfig(form.tts, voices)
    if (!hasUsableVoice(voices, reconciledTts.enabled_providers)) {
      toast.warning('至少保留一个可用音色')
      return
    }
    const normalized = normalizeConfig({ ...form, tts: reconciledTts })
    const issue = validateConfig(normalized, selectedLlmProvider)
    if (issue) {
      toast.warning(issue)
      return
    }
    setSaving(true)
    try {
      const saved = await updateConfig({ ...normalized, revision: configRevisionRef.current })
      configRevisionRef.current = saved.revision
      const savedConfig = normalizeConfig(saved || normalized)
      currentLlmProviderRef.current = savedConfig.llm.provider
      setForm(savedConfig)
      llmDraftsRef.current = {
        ...llmDraftsRef.current,
        [savedConfig.llm.provider]: {
          ...savedConfig.llm,
          provider_options: { ...(savedConfig.llm.provider_options || {}) },
        },
      }
      toast.success('配置已保存')
    } catch (error) {
      console.error('保存 API 配置失败', error)
      toast.error('保存 API 配置失败')
    } finally {
      setSaving(false)
    }
  }

  const testTts = async () => {
    setTtsTestUrl('')
    const issue = validateTtsTest({ ...form, tts: { ...form.tts, provider: providerTab } })
    if (issue) {
      toast.warning(issue)
      return
    }
    setTestingTts(true)
    try {
      const result = await testTtsConfig({
        tts: { ...form.tts, provider: providerTab },
        voice_type: defaultVoiceKey,
        text: 'InsightCut 配音配置测试成功。',
      })
      setTtsTestUrl(result?.url || '')
      toast.success('TTS 配置测试通过')
    } catch (error) {
      console.error('TTS 配置测试失败', error)
      toast.error(error?.response?.data?.detail || 'TTS 配置测试失败')
    } finally {
      setTestingTts(false)
    }
  }

  if (loading) return <main className="delivery-loading"><LoadingState label="正在读取 API 配置..." /></main>
  if (loadError) return <main className="delivery-loading"><EmptyState variant="configuration" eyebrow="配置连接" title="API 配置不可用" description={loadError} action={<button className="button button-primary" type="button" onClick={loadConfig}>重试</button>} /></main>

  return (
    <main className={`settings-page${embedded ? ' is-embedded' : ''}`}>
      <header className="settings-heading">
        <div><p className="eyebrow">配置控制台</p><h1>API 配置</h1><p>维护生文、生图和配音服务连接，配置会原样提交给本地后端。</p></div>
        <button className="button button-secondary" type="button" onClick={() => embedded ? onClose?.() : navigate(-1)}><ArrowLeft size={16} aria-hidden="true" />{embedded ? '关闭配置' : '返回生产'}</button>
      </header>

      <section className="settings-readiness" aria-label="配置状态">
        {readiness.map(item => <div key={item.label} className={item.ready ? 'is-ready' : 'is-warning'}>{item.ready ? <CheckCircle2 size={17} aria-hidden="true" /> : <CircleAlert size={17} aria-hidden="true" />}<span><strong>{item.label}</strong><small>{item.detail}</small></span></div>)}
      </section>

      <div className="settings-layout">
        <nav className="settings-section-nav" aria-label="配置章节">
          {SECTION_LINKS.map(({ id, label, icon: Icon }) => <a href={`#${id}`} key={id}><Icon size={16} aria-hidden="true" />{label}</a>)}
        </nav>

        <div className="settings-console">
          <ConfigSection id="settings-llm" icon={MessageSquareText} title="生文模型" badge="LLM" description="LiteLLM 统一调用层，支持 OpenAI 或 Anthropic 兼容协议。">
            <LlmProviderSettings
              value={form.llm}
              providers={llmProviders}
              models={displayedLlmModels}
              syncState={llmSyncState}
              onChange={updateLlm}
              onProviderChange={selectLlmProvider}
              onSync={syncLlmProviderModels}
            />
          </ConfigSection>

          <ConfigSection id="settings-image" icon={Image} title="Agnes 生图" badge="Image" description="真实调用 Agnes Image 2.1 Flash 图片生成接口。">
            <Field label="服务商 / 模型" wide group><span className="settings-fixed-summary">Agnes / <code>{AGNES_PRESET.model}</code></span></Field>
            <Field label="API Key"><input type="password" autoComplete="off" value={form.image.api_key} onChange={event => updateSection('image', 'api_key', event.target.value)} placeholder="sk-..." /></Field>
            <Field label="图片尺寸"><select value={form.image.size} onChange={event => updateSection('image', 'size', event.target.value)}>{IMAGE_SIZES.map(size => <option value={size} key={size}>{size === 'auto' ? '自动匹配画幅' : size}</option>)}</select></Field>
            <AdvancedSettings
              title="高级配置"
              summary={`${form.image.api_url || '未设置 URL'} · ${form.image.model || '未设置模型'}`}
              onRestore={() => setForm(current => restoreAgnesPreset(current))}
            >
              <label><span>API URL</span><input value={form.image.api_url} onChange={event => updateSection('image', 'api_url', event.target.value)} placeholder={AGNES_PRESET.api_url} /></label>
              <label><span>Model</span><input value={form.image.model} onChange={event => updateSection('image', 'model', event.target.value)} placeholder={AGNES_PRESET.model} /></label>
            </AdvancedSettings>
          </ConfigSection>

          <ConfigSection id="settings-tts" icon={Volume2} title="配音模型" badge="2 Providers" description="豆包与 MiMo 可同时启用；每个项目会保存音色和参数快照。">
            <div className="settings-provider-switches"><label><input type="checkbox" checked={form.tts.enabled_providers.includes('doubao')} onChange={event => setProviderEnabled('doubao', event.target.checked)} /><span><strong>豆包 TTS</strong><small>预置 10 个本地音色</small></span></label><label><input type="checkbox" checked={form.tts.enabled_providers.includes('mimo')} onChange={event => setProviderEnabled('mimo', event.target.checked)} /><span><strong>小米 MiMo</strong><small>9 个预置 + 本地声音克隆</small></span></label></div>
            <Field label="新项目默认 Provider" wide group><Segmented value={form.tts.provider} onChange={value => form.tts.enabled_providers.includes(value) ? updateTts('provider', value) : toast.warning('请先启用该 Provider')} options={[['doubao', '豆包 TTS'], ['mimo', '小米 MiMo TTS']]} /></Field>
            <Field label="当前编辑" wide group><Segmented value={providerTab} onChange={setProviderTab} options={[['doubao', '豆包配置'], ['mimo', 'MiMo 配置']]} /></Field>
            {providerTab === 'doubao'
              ? <DoubaoFields form={form} updateTts={updateTts} />
              : <MimoFields form={form} updateMimo={updateMimo} onRestore={() => setForm(current => restoreMimoTechnicalPreset(current))} />}
            <Field label="固定试听文案" wide><span className="settings-fixed-summary">欢迎来到 InsightCut，让我们一起把灵感变成精彩视频。</span></Field>
            <div className="settings-voice-library">
              <header><div><strong>{providerTab === 'mimo' ? 'MiMo' : '豆包'} 音色库</strong><small>全部预置音色都会出现在第二阶段；这里只设置新项目的默认音色。</small></div></header>
              <VoicePicker voices={providerVoices} value={defaultVoiceKey} ttsOptions={providerOptions} onChange={selectDefaultVoice} onOptionsChange={options => { stopPreview(); updateProviderOptions(options) }} onPreview={handleVoicePreview} playingVoice={previewState.playingVoice} previewLoading={previewState.loading} previewError={previewState.error} optionsProvider={providerTab} />
            </div>
            {providerTab === 'mimo' ? <div className="settings-clone-library">
              <header><div><strong>MiMo 声音克隆</strong><small>参考音频只保存在本地，试听成功后才能启用。</small></div></header>
              <div className="clone-create-panel">
                <label><span>音色名称</span><input value={cloneName} maxLength="80" onChange={event => setCloneName(event.target.value)} placeholder="例如：我的旁白声音" /></label>
                <label className="clone-file-control"><span>参考音频</span><input type="file" accept="audio/wav,audio/mpeg,audio/webm,.wav,.mp3,.webm" onChange={event => setCloneFile(event.target.files?.[0] || null)} /><small>{cloneFile ? cloneFile.name : '支持 MP3 / WAV，也可直接录音'}</small></label>
                <button className="button button-secondary" type="button" onClick={recording ? () => recorderRef.current?.stop() : startRecording}>{recording ? <Square size={15} /> : <Mic size={15} />}{recording ? '停止录音' : '浏览器录音'}</button>
                <label className="clone-consent"><input type="checkbox" checked={cloneConsent} onChange={event => setCloneConsent(event.target.checked)} /><span>我已获得该声音的使用授权</span></label>
                <button className="button button-primary" type="button" disabled={cloneBusy === 'create'} onClick={createClone}>{cloneBusy === 'create' ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />}创建并生成试听</button>
              </div>
              <div className="clone-record-list">
                {clones.filter(clone => clone.status !== 'hidden').map(clone => <article key={clone.clone_id} id={`clone-${clone.clone_id}`} tabIndex={-1}><div><strong>{clone.name}</strong><small>{clone.status === 'ready' ? '已就绪' : clone.status === 'failed' ? `试听失败·${clone.error_message || '可重试'}` : '待生成试听'}{clone.duration ? ` · ${Number(clone.duration).toFixed(1)}s` : ''}</small></div><span className="clone-record-actions"><button type="button" disabled={cloneBusy === `preview:${clone.clone_id}`} onClick={() => retryClonePreview(clone)}><Volume2 size={14} />试听</button><button type="button" disabled={clone.status !== 'ready'} onClick={() => patchClone(clone.clone_id, { is_enabled: !clone.is_enabled })}>{clone.is_enabled ? '停用' : '启用'}</button><button type="button" onClick={() => { const name = window.prompt('修改音色名称', clone.name); if (name?.trim()) patchClone(clone.clone_id, { name: name.trim() }) }}><Pencil size={14} />重命名</button><label><RefreshCw size={14} />替换<input type="file" accept="audio/wav,audio/mpeg,audio/webm,.wav,.mp3,.webm" onChange={event => replaceClone(clone.clone_id, event.target.files?.[0])} /></label><button type="button" onClick={() => setCloneToDelete(clone)}><Trash2 size={14} />删除</button></span></article>)}
                {!clones.filter(clone => clone.status !== 'hidden').length ? <EmptyStateCard variant="voice" eyebrow="本地音色" title="还没有克隆音色" description="使用上方表单上传或录制一段已获授权的参考音频。" compact /> : null}
              </div>
            </div> : null}
            <div className="settings-test-row">
              <button className="button button-secondary" type="button" disabled={testingTts} onClick={testTts}>{testingTts ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <Volume2 size={16} aria-hidden="true" />}{testingTts ? '测试中...' : '测试配音配置'}</button>
              {ttsTestUrl ? <audio src={normalizeMediaUrl(ttsTestUrl)} controls aria-label="TTS 配置测试音频" /> : <span>测试成功后可在此试听返回音频。</span>}
            </div>
          </ConfigSection>

          <ConfigSection id="settings-runtime" icon={Cpu} title="生成策略" badge="Runtime" description="并发和重试设置会保存为下个项目的默认值。">
            <Field label="提示词并发" help="范围 1-8，默认 4；每段仍独立生成，过高可能触发模型限流。"><input type="number" min="1" max="8" step="1" value={form.generation.prompt_concurrency} onChange={event => updateSection('generation', 'prompt_concurrency', event.target.value)} onBlur={() => updateSection('generation', 'prompt_concurrency', normalizeConcurrency(form.generation.prompt_concurrency, 4))} /></Field>
            <Field label="配音并发" help="范围 1-8；调高更快，也更容易触发 provider 限流。"><input type="number" min="1" max="8" step="1" value={form.generation.tts_concurrency} onChange={event => updateSection('generation', 'tts_concurrency', event.target.value)} onBlur={() => updateSection('generation', 'tts_concurrency', normalizeConcurrency(form.generation.tts_concurrency))} /></Field>
            <Field label="生图并发" help="范围 1-8，默认 8；免费账号实测支持 8 路突发，滚动 60 秒最多请求 20 次。"><input type="number" min="1" max="8" step="1" value={form.generation.image_concurrency} onChange={event => updateSection('generation', 'image_concurrency', event.target.value)} onBlur={() => updateSection('generation', 'image_concurrency', normalizeConcurrency(form.generation.image_concurrency, 8))} /></Field>
            <Field label="失败重试" help="失败后额外重试 0-5 次，默认 2；限流时仍优先遵循服务商等待时间。"><input type="number" min="0" max="5" step="1" value={form.generation.retry_count} onChange={event => updateSection('generation', 'retry_count', event.target.value)} onBlur={() => updateSection('generation', 'retry_count', normalizeRetryCount(form.generation.retry_count))} /></Field>
            <Field label="重试间隔" help="基础间隔 1-60 秒，默认 5 秒；普通失败逐次递增，限流时优先遵循服务商等待时间。"><input type="number" min="1" max="60" step="1" value={form.generation.retry_interval_seconds} onChange={event => updateSection('generation', 'retry_interval_seconds', event.target.value)} onBlur={() => updateSection('generation', 'retry_interval_seconds', normalizeRetryInterval(form.generation.retry_interval_seconds))} /></Field>
          </ConfigSection>
        </div>
      </div>

      <footer className="settings-actions"><button className="button button-secondary" type="button" disabled={saving} onClick={loadConfig}><RefreshCw size={16} aria-hidden="true" />重置</button><button className="button button-primary" type="button" disabled={saving} onClick={saveConfig}>{saving ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <Save size={16} aria-hidden="true" />}{saving ? '保存中...' : '保存配置'}</button></footer>
      <ConfirmDialog
        open={Boolean(cloneToDelete)}
        title="删除克隆音色"
        message={`确定处理“${cloneToDelete?.name || '这个音色'}”吗？未被项目使用的参考文件会删除；已被项目引用的音色只会隐藏，历史项目仍可正常播放。`}
        confirmLabel={cloneBusy === `delete:${cloneToDelete?.clone_id}` ? '正在处理…' : '确认删除'}
        confirmDisabled={cloneBusy === `delete:${cloneToDelete?.clone_id}`}
        danger
        onConfirm={removeClone}
        onClose={() => { if (cloneBusy !== `delete:${cloneToDelete?.clone_id}`) setCloneToDelete(null) }}
      />
    </main>
  )
}

function ConfigSection({ id, icon: Icon, title, badge, description, children }) {
  return <section className="settings-section" id={id}><header><span><Icon size={18} aria-hidden="true" /></span><div><h2>{title}</h2><p>{description}</p></div><strong>{badge}</strong></header><div className="settings-fields">{children}</div></section>
}

function Field({ label, help, wide = false, group = false, children }) {
  const Tag = group ? 'div' : 'label'
  return <Tag className={`settings-field${wide ? ' is-wide' : ''}`}><span>{label}</span>{children}{help && <small>{help}</small>}</Tag>
}

function Segmented({ value, options, onChange }) {
  return <span className="settings-segmented">{options.map(([key, label]) => <button type="button" className={value === key ? 'is-active' : ''} onClick={() => onChange(key)} key={key}>{label}</button>)}</span>
}

function DoubaoFields({ form, updateTts }) {
  return <>
    <Field label="豆包认证方式" wide group help="旧版在线合成使用 AppID / Access Token；火山新版语音接口也支持 API Key。"><Segmented value={form.tts.auth_method} onChange={value => updateTts('auth_method', value)} options={[['access_token', 'AppID / Access Token'], ['api_key', 'API Key']]} /></Field>
    {form.tts.auth_method === 'access_token' ? <><Field label="App ID"><input value={form.tts.appid} onChange={event => updateTts('appid', event.target.value)} placeholder="豆包 TTS App ID" /></Field><Field label="Access Token"><input type="password" autoComplete="off" value={form.tts.token} onChange={event => updateTts('token', event.target.value)} placeholder="豆包 Access Token" /></Field></> : <Field label="API Key"><input type="password" autoComplete="off" value={form.tts.api_key} onChange={event => updateTts('api_key', event.target.value)} placeholder="火山控制台 API Key" /></Field>}
    <AdvancedSettings title="高级配置" summary={`${form.tts.api_url || '未设置 URL'} · ${form.tts.cluster || '未设置 Cluster'}`}>
      <label><span>API URL</span><input value={form.tts.api_url} onChange={event => updateTts('api_url', event.target.value)} placeholder="https://openspeech.bytedance.com/api/v1/tts" /></label>
      <label><span>Cluster</span><input value={form.tts.cluster} onChange={event => updateTts('cluster', event.target.value)} placeholder="volcano_tts" /></label>
    </AdvancedSettings>
  </>
}

function MimoFields({ form, updateMimo, onRestore }) {
  return <>
    <Field label="API Key"><input type="password" autoComplete="off" value={form.tts.mimo.api_key} onChange={event => updateMimo('api_key', event.target.value)} placeholder="小米 Token Plan API Key" /></Field>
    <AdvancedSettings title="高级配置" summary={`${form.tts.mimo.model || '未设置 TTS 模型'} · ${form.tts.mimo.format || '未设置格式'}`} onRestore={onRestore}>
      <label><span>Base URL</span><input value={form.tts.mimo.base_url} onChange={event => updateMimo('base_url', event.target.value)} placeholder="https://token-plan-sgp.xiaomimimo.com/v1" /></label>
      <label><span>TTS Model</span><input value={form.tts.mimo.model} onChange={event => updateMimo('model', event.target.value)} placeholder="mimo-v2.5-tts" /></label>
      <label><span>克隆 Model</span><input value={form.tts.mimo.clone_model} onChange={event => updateMimo('clone_model', event.target.value)} placeholder="mimo-v2.5-tts-voiceclone" /></label>
      <label><span>音频格式</span><input value={form.tts.mimo.format} onChange={event => updateMimo('format', event.target.value)} placeholder="wav" /></label>
    </AdvancedSettings>
  </>
}
