import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Clock3,
  ImageOff,
  LoaderCircle,
  Pause,
  Play,
  RefreshCw,
  Sparkles,
  Upload,
  WandSparkles,
} from 'lucide-react'
import { useLocation, useNavigate, useParams } from 'react-router'
import {
  cancelExportJob,
  createExport,
  generateTaskWorkspaceAssets,
  getConfig,
  getExportJob,
  getExportState,
  getTaskAssets,
  getTaskWorkspace,
  getVoices,
  previewVoice,
  regenerateSegmentPrompt,
  retryTaskAssets,
  finalizeTaskWorkspace,
  resegmentTaskWorkspace,
  resumeTask,
  selectSegmentImage,
  updateSegment,
  updateConfig,
  updateTaskWorkspaceSettings,
  uploadImage,
} from '../api/task'
import { ConfirmDialog, Modal } from '../components/Modal'
import { PollingFailureNotice } from '../components/PollingFailureNotice'
import { WorkspaceFailureBanner } from '../components/WorkspaceFailureBanner'
import { WorkspaceActionBar } from '../components/WorkspaceActionBar'
import { WorkspaceInspector } from '../components/WorkspaceInspector'
import { WorkspaceSettingsOverlay } from '../components/WorkspaceSettingsOverlay'
import { WorkspaceStoryboardNav } from '../components/WorkspaceStoryboardNav'
import { WorkspaceStageNavigator } from '../components/WorkspaceStageNavigator'
import { BrandLoader } from '../components/ui/BrandLoader'
import { Lightbox } from '../components/ui/Lightbox'
import { usePollingResource } from '../hooks/usePollingResource'
import { errorToastMessage, getErrorPresentation } from '../lib/errorMessages'
import { normalizeConcurrency, normalizeRetryCount, normalizeRetryInterval } from '../lib/settingsConfig'
import { toast } from '../lib/toast'
import { clearSelectedProject, selectProject } from '../lib/projectSelection'
import { mergeTtsOptions, nextPreviewState, normalizeVoiceCatalog } from '../lib/voiceCatalog'
import { normalizeMediaUrl } from '../utils/mediaUrl'
import { ratioOptions, visualStyles } from '../utils/projectDrafts'
import { ratioClassName } from '../utils/taskState'
import { normalizeSubtitleText, secondsToLabel, segmentDuration } from './previewUtils'
import { SettingsPage } from './SettingsPage'
import {
  deriveWorkspaceControls,
  isSegmentPreviewReady,
  nextPreviewIndex,
  previewPlaybackStartIndex,
  recoveryActionForWorkspace,
} from './workspacePreview'
import { buildFailedRetryPayload, collectWorkspaceIssues, deriveWorkspaceJourney } from './workspaceGuidance'
import { readWorkspaceView, writeWorkspaceView } from './workspaceViewState'
import './workspace-page.css'

const LAST_VOICE_KEY = 'insightcut:last-voice'
const LAST_TTS_OPTIONS_KEY = 'insightcut:last-tts-options'
const LAST_RUNTIME_KEY = 'insightcut:last-generation-runtime'
const ACTIVE_EXPORT_STATUSES = new Set(['pending', 'processing'])
const WORKSPACE_TOUR_KEY = 'insightcut:workspace-tour:v1'

function shouldShowWorkspaceTour() {
  try {
    return localStorage.getItem(WORKSPACE_TOUR_KEY) !== 'seen'
  } catch {
    return true
  }
}

function activePreviewJob(exportState) {
  return (Array.isArray(exportState?.jobs) ? exportState.jobs : [])
    .find(job => job.target === 'mp4' && ACTIVE_EXPORT_STATUSES.has(job.status)) || null
}

function normalizeRuntimeConfig(value = {}) {
  return {
    prompt_concurrency: normalizeConcurrency(value.prompt_concurrency, 4),
    tts_concurrency: normalizeConcurrency(value.tts_concurrency, 1),
    image_concurrency: normalizeConcurrency(value.image_concurrency, 8),
    retry_count: normalizeRetryCount(value.retry_count, 2),
    retry_interval_seconds: normalizeRetryInterval(value.retry_interval_seconds, 5),
  }
}

function readRuntimeConfig() {
  try {
    return normalizeRuntimeConfig(JSON.parse(localStorage.getItem(LAST_RUNTIME_KEY) || '{}'))
  } catch {
    return normalizeRuntimeConfig()
  }
}

function readLastTtsOptions() {
  try {
    const parsed = JSON.parse(localStorage.getItem(LAST_TTS_OPTIONS_KEY) || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function pendingKey(taskId) {
  return `insightcut:workspace-pending:${taskId}`
}

function readPending(taskId) {
  try {
    const parsed = JSON.parse(localStorage.getItem(pendingKey(taskId)) || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writePending(taskId, value) {
  if (Object.keys(value).length) localStorage.setItem(pendingKey(taskId), JSON.stringify(value))
  else localStorage.removeItem(pendingKey(taskId))
}

function stageMeta(workspace) {
  const stage = workspace?.stage
  const error = getErrorPresentation({
    error_code: workspace?.error_code,
    error_meta: workspace?.error_meta,
  })
  if (stage === 'planning') {
    const ready = workspace?.progress?.prompts_ready || 0
    const total = workspace?.progress?.prompts_total || workspace?.segments_count || 0
    if (workspace?.planning_step === 'image_prompt_generation') {
      return {
        title: `正在生成提示词 ${ready}/${total}`,
        description: '每个分镜独立生成，完成的内容会立即填入工作台',
        tone: 'working',
      }
    }
    if (workspace?.planning_step === 'segmentation') {
      return { title: '正在拆分分镜', description: '完整文案已保存，正在按口播节奏拆分画面', tone: 'working' }
    }
    return { title: '正在扩写完整文案', description: '文案返回后会立即展示，无需等待全部提示词', tone: 'working' }
  }
  const states = {
    awaiting_confirmation: ['预案等待确认', '检查文案、提示词与音色后再生成素材', 'review'],
    generating_assets: ['正在生成图片与配音', '完成的素材会逐段填入左侧预览', 'working'],
    repairing_assets: ['正在修复失败素材', '只处理指定的图片或配音，其他素材保持不变', 'working'],
    awaiting_finalization: ['素材已经齐全', '确认后只构建生产草稿，不会再次调用生成模型', 'review'],
    finalizing: ['正在完成生产', '正在使用现有图片与配音构建生产草稿', 'working'],
    ready: ['素材已就绪', '可以即时预览、继续修改或按需生成完整视频', 'ready'],
    interrupted: ['生成已暂停', `${error.title}。${error.action}`, 'warning'],
    failed: ['部分流程失败', `${error.title}。${error.action}`, 'warning'],
  }
  const [title, description, tone] = states[stage] || states.interrupted
  return { title, description, tone }
}

function segmentState(segment) {
  if (!segment) return { label: '等待分镜', tone: 'pending' }
  if (segment.image_status === 'failed' || segment.audio_status === 'failed' || segment.prompt_status === 'failed') {
    return { label: '需要处理', tone: 'error' }
  }
  if (segment.image_status === 'stale' || segment.audio_status === 'stale' || segment.prompt_needs_review) {
    return { label: '待更新', tone: 'warning' }
  }
  if (segment.image_status === 'completed' && segment.audio_status === 'completed') {
    return { label: '素材完成', tone: 'ready' }
  }
  if (segment.prompt_status === 'processing' || segment.image_status === 'processing' || segment.audio_status === 'processing') {
    return { label: '生成中', tone: 'working' }
  }
  return { label: segment.image_prompt ? '等待素材' : '等待提示词', tone: 'pending' }
}

function formatEstimate(estimate) {
  const min = Math.max(1, Math.ceil(Number(estimate?.min_seconds || 0) / 60))
  const max = Math.max(min, Math.ceil(Number(estimate?.max_seconds || 0) / 60))
  return `${min}–${max} 分钟`
}

function historyTimestamp(value) {
  if (!value) return '时间未知'
  const timestamp = new Date(String(value).replace(' ', 'T'))
  if (Number.isNaN(timestamp.getTime())) return String(value)
  return timestamp.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function preloadPreviewImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = resolve
    image.onerror = reject
    image.src = url
    if (image.complete && image.naturalWidth) resolve()
  })
}

function waitForPreviewPaint() {
  return new Promise(resolve => window.requestAnimationFrame(() => window.requestAnimationFrame(resolve)))
}

export function WorkspacePage() {
  const { taskId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const isSettingsOverlay = location.pathname.endsWith('/settings')
  const [workspace, setWorkspace] = useState(null)
  const workspaceRef = useRef(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [missingTask, setMissingTask] = useState(false)
  const [selectedIndex, setSelectedIndex] = useState(() => readWorkspaceView(taskId).selectedIndex)
  const [settingsOpen, setSettingsOpen] = useState(() => readWorkspaceView(taskId).settingsOpen)
  const [mobilePane, setMobilePane] = useState(() => readWorkspaceView(taskId).mobilePane)
  const [voices, setVoices] = useState([])
  const [selectedVoice, setSelectedVoice] = useState('')
  const [ttsOptions, setTtsOptions] = useState({ speed_level: 'normal' })
  const [runtimeConfig, setRuntimeConfig] = useState(readRuntimeConfig)
  const [voicePreviewState, setVoicePreviewState] = useState(() => nextPreviewState())
  const voiceAudioRef = useRef(null)
  const previewTokenRef = useRef(0)
  const playbackAudioRef = useRef(null)
  const playbackRunRef = useRef(0)
  const playbackEndedNaturallyRef = useRef(false)
  const fullVideoRef = useRef(null)
  const [playing, setPlaying] = useState(false)
  const [previewMode, setPreviewMode] = useState('content')
  const [exportState, setExportState] = useState(null)
  const [previewJob, setPreviewJob] = useState(null)
  const [busyAction, setBusyAction] = useState('')
  const [resegmentConfirmOpen, setResegmentConfirmOpen] = useState(false)
  const [tourOpen, setTourOpen] = useState(shouldShowWorkspaceTour)
  const [historySegmentIndex, setHistorySegmentIndex] = useState(null)
  const [imageHistories, setImageHistories] = useState({})
  const [historyLoading, setHistoryLoading] = useState(false)
  const [lightboxOpen, setLightboxOpen] = useState(false)
  const [lightboxIndex, setLightboxIndex] = useState(0)
  const uploadInputRef = useRef(null)
  const uploadSegmentRef = useRef(null)
  const [savingCount, setSavingCount] = useState(0)
  const [saveMessage, setSaveMessage] = useState('已同步')
  const saveTimersRef = useRef(new Map())
  const saveQueueRef = useRef(Promise.resolve())
  const saveGenerationRef = useRef(0)
  const pendingRef = useRef(readPending(taskId))
  const initializedTaskRef = useRef(null)
  const activeTaskIdRef = useRef(taskId)
  const notifiedPreviewJobsRef = useRef(new Set())
  const scrollPaneRefs = useRef({ storyboard: null, preview: null, segmentInspector: null, fullSettings: null })
  const scrollPositionsRef = useRef(readWorkspaceView(taskId).scroll)
  const scrollSaveTimerRef = useRef(null)

  activeTaskIdRef.current = taskId
  useEffect(() => { workspaceRef.current = workspace }, [workspace])

  useEffect(() => {
    const rememberedView = readWorkspaceView(taskId)
    saveGenerationRef.current += 1
    saveTimersRef.current.forEach(timer => window.clearTimeout(timer))
    saveTimersRef.current.clear()
    saveQueueRef.current = Promise.resolve()
    pendingRef.current = readPending(taskId)
    initializedTaskRef.current = null
    workspaceRef.current = null
    setWorkspace(null)
    setLoading(true)
    setLoadError('')
    setMissingTask(false)
    setSavingCount(0)
    setSaveMessage('已同步')
    setSelectedIndex(rememberedView.selectedIndex)
    setSettingsOpen(rememberedView.settingsOpen)
    setMobilePane(rememberedView.mobilePane)
    scrollPositionsRef.current = rememberedView.scroll
    setExportState(null)
    setPreviewJob(null)
    setHistorySegmentIndex(null)
    setImageHistories({})
    setHistoryLoading(false)
    setLightboxOpen(false)
    setLightboxIndex(0)
    uploadSegmentRef.current = null
  }, [taskId])

  const setWorkspaceSettingsOpen = useCallback(nextValue => {
    setSettingsOpen(current => {
      const next = typeof nextValue === 'function' ? Boolean(nextValue(current)) : Boolean(nextValue)
      writeWorkspaceView(taskId, { settingsOpen: next })
      return next
    })
  }, [taskId])

  const selectMobilePane = useCallback(nextPane => {
    setMobilePane(nextPane)
    writeWorkspaceView(taskId, { mobilePane: nextPane })
  }, [taskId])

  const rememberPaneScroll = useCallback((pane, event) => {
    const scrollTop = Math.max(0, Math.round(event.currentTarget.scrollTop || 0))
    scrollPositionsRef.current = { ...scrollPositionsRef.current, [pane]: scrollTop }
    window.clearTimeout(scrollSaveTimerRef.current)
    scrollSaveTimerRef.current = window.setTimeout(() => {
      writeWorkspaceView(taskId, { scroll: scrollPositionsRef.current })
    }, 120)
  }, [taskId])

  const storyboardScrollRef = useCallback(node => { scrollPaneRefs.current.storyboard = node }, [])
  const previewScrollRef = useCallback(node => { scrollPaneRefs.current.preview = node }, [])
  const segmentInspectorScrollRef = useCallback(node => { scrollPaneRefs.current.segmentInspector = node }, [])
  const fullSettingsScrollRef = useCallback(node => { scrollPaneRefs.current.fullSettings = node }, [])

  useEffect(() => {
    if (loading || !workspaceRef.current) return undefined
    const frame = window.requestAnimationFrame(() => {
      Object.entries(scrollPaneRefs.current).forEach(([pane, node]) => {
        if (node) node.scrollTop = scrollPositionsRef.current[pane] || 0
      })
    })
    return () => window.cancelAnimationFrame(frame)
  }, [loading, taskId])

  useEffect(() => {
    if (loading) return undefined
    const pane = settingsOpen ? 'fullSettings' : 'segmentInspector'
    const frame = window.requestAnimationFrame(() => {
      const node = scrollPaneRefs.current[pane]
      if (node) node.scrollTop = scrollPositionsRef.current[pane] || 0
    })
    return () => window.cancelAnimationFrame(frame)
  }, [loading, settingsOpen, taskId])

  useEffect(() => () => {
    window.clearTimeout(scrollSaveTimerRef.current)
    writeWorkspaceView(taskId, { scroll: scrollPositionsRef.current })
  }, [taskId])

  const applyPendingEdits = useCallback(data => {
    const pending = pendingRef.current
    if (!Object.keys(pending).length) return data
    return {
      ...data,
      segments: (data.segments || []).map(segment => ({
        ...segment,
        ...(pending[segment.segment_index] || {}),
      })),
    }
  }, [])

  const applyWorkspaceData = useCallback(rawData => {
    const data = applyPendingEdits(rawData)
    setWorkspace(data)
    workspaceRef.current = data
    setSelectedIndex(current => Math.max(0, Math.min(current, Math.max((data.segments?.length || 1) - 1, 0))))

    if (initializedTaskRef.current !== taskId) {
      initializedTaskRef.current = taskId
      const rememberedView = readWorkspaceView(taskId)
      const rememberedVoice = localStorage.getItem(LAST_VOICE_KEY) || ''
      const hasConfirmedVoice = Boolean(data.voice_confirmed && data.voice_type)
      const initialVoice = data.voice_type || rememberedVoice
      const initialOptions = hasConfirmedVoice ? data.tts_options || {} : readLastTtsOptions()
      setSelectedVoice(initialVoice)
      setTtsOptions(mergeTtsOptions({}, initialOptions, String(initialVoice).startsWith('doubao:') ? 'doubao' : 'mimo'))
      setWorkspaceSettingsOpen(hasConfirmedVoice ? rememberedView.settingsOpen : true)
      if (!hasConfirmedVoice && window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('settings')
    }

    selectProject({ taskId, name: data.name })
    setLoading(false)
    setLoadError('')
    setMissingTask(false)
  }, [applyPendingEdits, selectMobilePane, setWorkspaceSettingsOpen, taskId])

  const handleMissingTask = useCallback(() => {
    clearSelectedProject(taskId)
    setMissingTask(true)
    setWorkspace(null)
    workspaceRef.current = null
    setLoadError('项目不存在或已经被删除')
    setLoading(false)
  }, [taskId])

  const workspacePolling = usePollingResource({
    resourceKey: taskId,
    enabled: !missingTask,
    interval: 1500,
    request: (currentTaskId, { signal }) => getTaskWorkspace(currentTaskId, { silent: true, signal }),
    isTerminalError: error => error?.response?.status === 404,
    onTerminalError: handleMissingTask,
    onData: applyWorkspaceData,
    onError: (error, metadata) => {
      if (!metadata.persistent) return
      setLoading(false)
      if (!workspaceRef.current) setLoadError(errorToastMessage(error))
    },
  })

  useEffect(() => {
    let active = true
    Promise.all([
      getVoices({ include_disabled: true }).catch(() => []),
      getExportState(taskId, { silent: true }).catch(() => null),
      getConfig().catch(() => null),
    ]).then(([voiceList, nextExport, config]) => {
      if (!active) return
      setVoices(normalizeVoiceCatalog(voiceList))
      setExportState(nextExport)
      setPreviewJob(current => (
        current && ACTIVE_EXPORT_STATUSES.has(current.status)
          ? current
          : activePreviewJob(nextExport)
      ))
      if (config?.generation) {
        const normalized = normalizeRuntimeConfig(config.generation)
        setRuntimeConfig(normalized)
        localStorage.setItem(LAST_RUNTIME_KEY, JSON.stringify(normalized))
      }
    })
    return () => { active = false }
  }, [taskId]) // eslint-disable-line react-hooks/exhaustive-deps

  const previewJobIsActive = Boolean(
    previewJob
    && (!previewJob.task_id || previewJob.task_id === taskId)
    && ACTIVE_EXPORT_STATUSES.has(previewJob.status),
  )
  const previewPollingKey = previewJobIsActive
    ? JSON.stringify([taskId, previewJob.job_id])
    : null
  const previewJobPolling = usePollingResource({
    resourceKey: previewPollingKey,
    enabled: Boolean(previewPollingKey),
    interval: 1800,
    request: (key, { signal }) => {
      const [currentTaskId, jobId] = JSON.parse(key)
      return getExportJob(currentTaskId, jobId, { silent: true, signal })
    },
    onData: next => {
      setPreviewJob(next)
      const notificationKey = `${next.job_id}:${next.status}`
      if (next.status === 'completed') {
        setPreviewMode('full')
        if (!notifiedPreviewJobsRef.current.has(notificationKey)) {
          notifiedPreviewJobsRef.current.add(notificationKey)
          toast.success('完整视频预览已生成')
        }
        const expectedTaskId = taskId
        void getExportState(expectedTaskId, { silent: true }).then(state => {
          if (activeTaskIdRef.current === expectedTaskId) setExportState(state)
        }).catch(() => {})
      } else if (next.status === 'failed' && !notifiedPreviewJobsRef.current.has(notificationKey)) {
        notifiedPreviewJobsRef.current.add(notificationKey)
        toast.error(errorToastMessage(next))
      } else if (next.status === 'cancelled' && !notifiedPreviewJobsRef.current.has(notificationKey)) {
        notifiedPreviewJobsRef.current.add(notificationKey)
        setPreviewMode(fullVideoUrl ? 'full' : 'content')
        toast.info('完整视频生成已取消，上一份可用视频仍然保留')
      }
    },
  })

  useEffect(() => () => {
    saveTimersRef.current.forEach(timer => window.clearTimeout(timer))
    playbackAudioRef.current?.pause()
    voiceAudioRef.current?.pause()
    fullVideoRef.current?.pause()
  }, [])

  const segments = workspace?.segments || []
  const currentSegment = segments[selectedIndex] || null
  const imageUrl = normalizeMediaUrl(currentSegment?.image_url)
  const lightboxItems = useMemo(() => (workspace?.segments || []).map((segment, segmentPosition) => ({
    id: segment.id || segment.segment_index,
    src: normalizeMediaUrl(segment.image_url),
    alt: `分镜 ${segmentPosition + 1} 画面`,
    title: `分镜 ${segmentPosition + 1}`,
    prompt: segment.image_prompt,
    segment,
    segmentPosition,
  })).filter(item => item.src), [workspace?.segments])
  const currentImageHistory = currentSegment ? imageHistories[currentSegment.segment_index] || [] : []
  const currentHistoryVersions = currentImageHistory.map((asset, index) => ({
    id: asset.asset_id,
    label: asset.label || `${asset.source === 'upload' ? '上传' : asset.source === 'regenerated' ? '重生成' : '生成'}版本 ${index + 1}`,
    createdAt: historyTimestamp(asset.created_at),
    thumbnail: normalizeMediaUrl(asset.file_url || asset.url),
    status: asset.has_file ? 'complete' : 'failed',
    source: asset.source,
    restorable: Boolean(asset.has_file),
    raw: asset,
  }))
  const selectedHistoryId = currentImageHistory.find(asset => (
    asset.path && asset.path === currentSegment?.image_path
  ) || (
    normalizeMediaUrl(asset.url) && normalizeMediaUrl(asset.url) === imageUrl
  ))?.asset_id
  const fullVideoUrl = normalizeMediaUrl(
    exportState?.preview?.valid
      ? exportState?.preview?.manifest?.preview_url || exportState?.render?.video_url
      : exportState?.outputs?.mp4?.available ? exportState?.outputs?.mp4?.url : '',
  )
  const isRenderingFullVideo = Boolean(
    busyAction === 'preview'
    || (previewJobIsActive && !previewJobPolling.error),
  )
  const activeStyle = visualStyles.find(style => style.value === workspace?.visual_style) || visualStyles[0]
  const stage = stageMeta(workspace)
  const journey = useMemo(() => deriveWorkspaceJourney(workspace || {}), [workspace])
  const workspaceIssues = useMemo(() => collectWorkspaceIssues(workspace || {}), [workspace])
  const voiceReady = Boolean(workspace?.voice_confirmed && workspace?.voice_type)
  const visualPlanReady = Boolean(
    segments.length
    && workspace?.visual_style
    && workspace?.ratio
    && segments.every(segment => segment.prompt_status === 'completed' && segment.image_prompt),
  )
  const {
    isRecoverable: isRecoverableStage,
    canResume,
    recoveryLabel: recoveryActionLabel,
    recoverySummary,
    canEnterExport,
    canRenderFullVideo,
  } = deriveWorkspaceControls(workspace || {})
  const isPlanning = workspace?.stage === 'planning'
  const operationRunning = ['pending', 'running'].includes(workspace?.active_operation?.status)
  const editable = !isPlanning && !operationRunning && !['generating_assets', 'repairing_assets', 'finalizing'].includes(workspace?.stage)
  const operationTarget = useCallback((segment, assetType) => (
    workspace?.active_operation?.targets?.find(target => (
      Number(target.segment_index) === Number(segment.segment_index)
      && target.asset_type === assetType
      && ['pending', 'processing'].includes(target.status)
    ))
  ), [workspace?.active_operation])

  const selectSegment = useCallback(index => {
    const segmentCount = workspaceRef.current?.segments?.length || 0
    const safe = Math.max(0, Math.min(Number(index), Math.max(segmentCount - 1, 0)))
    setSelectedIndex(safe)
    playbackEndedNaturallyRef.current = false
    writeWorkspaceView(taskId, { selectedIndex: safe })
  }, [taskId])

  useEffect(() => {
    if (!workspace) return
    const focus = Number(new URLSearchParams(location.search).get('focus'))
    if (focus === 2) previewScrollRef.current?.scrollTo({ top: 0 })
    if (focus === 3) {
      setWorkspaceSettingsOpen(true)
      if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('settings')
    }
    if (focus === 4) {
      setWorkspaceSettingsOpen(false)
      if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('storyboard')
    }
    if (focus === 5) {
      const target = workspace.segments.findIndex(segment => (
        ['pending', 'processing', 'failed', 'stale'].includes(segment.image_status)
        || ['pending', 'processing', 'failed', 'stale'].includes(segment.audio_status)
      ))
      if (target >= 0) selectSegment(target)
    }
  }, [location.search, selectSegment, workspace?.task_id])

  const stopPlayback = useCallback(({ naturalEnd = false } = {}) => {
    playbackRunRef.current += 1
    playbackEndedNaturallyRef.current = naturalEnd
    playbackAudioRef.current?.pause()
    playbackAudioRef.current = null
    setPlaying(false)
  }, [])

  const playFrom = useCallback(async (index, runId) => {
    const segment = (workspaceRef.current?.segments || [])[index]
    if (!segment) return stopPlayback()
    if (!isSegmentPreviewReady(segment)) {
      toast.info(`第 ${index + 1} 段的图片或配音尚未生成，连续预览已停在这里`)
      return stopPlayback()
    }

    const imageUrl = normalizeMediaUrl(segment.image_url)
    try {
      await preloadPreviewImage(imageUrl)
    } catch {
      if (playbackRunRef.current !== runId) return
      toast.warning(`第 ${index + 1} 段画面加载失败，连续预览已停止`)
      return stopPlayback()
    }
    if (playbackRunRef.current !== runId) return

    selectSegment(index)
    await waitForPreviewPaint()
    if (playbackRunRef.current !== runId) return

    const next = () => {
      const all = workspaceRef.current?.segments || []
      if (index + 1 >= all.length) return stopPlayback({ naturalEnd: true })
      const nextIndex = nextPreviewIndex(all, index)
      if (nextIndex === null) {
        toast.info(`第 ${index + 2} 段素材尚未完成，连续预览已停止`)
        return stopPlayback()
      }
      void playFrom(nextIndex, runId)
    }
    const audioUrl = normalizeMediaUrl(segment.audio_url)
    const audio = new Audio(audioUrl)
    playbackAudioRef.current = audio
    audio.onended = next
    audio.onerror = () => {
      toast.warning(`第 ${index + 1} 段配音加载失败，连续预览已停止`)
      stopPlayback()
    }
    audio.play().catch(() => {
      toast.warning('浏览器阻止了音频播放，请再次点击播放')
      stopPlayback()
    })
  }, [selectSegment, stopPlayback])

  const togglePlayback = () => {
    if (previewMode === 'full' && fullVideoUrl) {
      const video = fullVideoRef.current
      if (!video) return
      if (!video.paused) {
        video.pause()
        return
      }
      if (video.ended || video.currentTime >= video.duration) video.currentTime = 0
      video.play().catch(() => toast.warning('浏览器阻止了视频播放，请再次点击播放'))
      return
    }
    if (playing) stopPlayback()
    else {
      const startIndex = previewPlaybackStartIndex(segments, selectedIndex, playbackEndedNaturallyRef.current)
      if (startIndex === null) return
      const runId = playbackRunRef.current + 1
      playbackRunRef.current = runId
      playbackEndedNaturallyRef.current = false
      setPlaying(true)
      void playFrom(startIndex, runId)
    }
  }

  const changePreviewMode = nextMode => {
    if (nextMode === previewMode) return
    if (previewMode === 'content') stopPlayback()
    else {
      fullVideoRef.current?.pause()
      setPlaying(false)
    }
    setPreviewMode(nextMode)
  }

  const updateLocalSegment = (segmentIndex, patch) => {
    setWorkspace(current => {
      const next = {
        ...current,
        segments: current.segments.map(segment => segment.segment_index === segmentIndex ? { ...segment, ...patch } : segment),
      }
      workspaceRef.current = next
      return next
    })
    pendingRef.current = {
      ...pendingRef.current,
      [segmentIndex]: { ...(pendingRef.current[segmentIndex] || {}), ...patch },
    }
    writePending(taskId, pendingRef.current)
  }

  const enqueueSave = (segmentIndex, patch) => {
    updateLocalSegment(segmentIndex, patch)
    const saveGeneration = saveGenerationRef.current
    const saveTaskId = taskId
    const timerKey = `${segmentIndex}:${Object.keys(patch).sort().join(',')}`
    window.clearTimeout(saveTimersRef.current.get(timerKey))
    setSaveMessage('等待保存…')
    saveTimersRef.current.set(timerKey, window.setTimeout(() => {
      setSavingCount(count => count + 1)
      setSaveMessage('正在保存…')
      saveQueueRef.current = saveQueueRef.current.catch(() => {}).then(async () => {
        if (saveGenerationRef.current !== saveGeneration || activeTaskIdRef.current !== saveTaskId) return
        const live = workspaceRef.current
        try {
          const result = await updateSegment(saveTaskId, segmentIndex, {
            ...patch,
            expected_plan_version: live?.plan_version,
          })
          if (saveGenerationRef.current !== saveGeneration || activeTaskIdRef.current !== saveTaskId) return
          pendingRef.current = { ...pendingRef.current }
          delete pendingRef.current[segmentIndex]
          writePending(saveTaskId, pendingRef.current)
          setWorkspace(current => {
            const next = { ...current, plan_version: result.plan_version, snapshot_key: result.snapshot_key }
            workspaceRef.current = next
            return next
          })
          setSaveMessage('已同步')
        } catch (error) {
          if (saveGenerationRef.current !== saveGeneration || activeTaskIdRef.current !== saveTaskId) return
          setSaveMessage('保存失败')
          toast.error(errorToastMessage(error))
          if (error?.response?.status === 409) workspacePolling.refresh()
        } finally {
          if (saveGenerationRef.current === saveGeneration && activeTaskIdRef.current === saveTaskId) {
            setSavingCount(count => Math.max(0, count - 1))
          }
        }
      })
    }, 320))
  }

  const stopVoicePreview = useCallback(() => {
    voiceAudioRef.current?.pause()
    voiceAudioRef.current = null
    setVoicePreviewState(current => nextPreviewState(current, { type: 'stop' }))
  }, [])

  const previewSelectedVoice = async (voice, optionsOverride = ttsOptions) => {
    if (voicePreviewState.playingVoice === voice.id) return stopVoicePreview()
    stopVoicePreview()
    const token = ++previewTokenRef.current
    setVoicePreviewState(current => nextPreviewState(current, { type: 'start', voiceId: voice.id, token }))
    try {
      const result = await previewVoice({ voice_type: voice.id, tts_options: optionsOverride })
      const audio = new Audio(normalizeMediaUrl(result.url))
      voiceAudioRef.current = audio
      audio.onended = stopVoicePreview
      audio.onerror = () => setVoicePreviewState(current => nextPreviewState(current, { type: 'error', voiceId: voice.id, token, error: '试听播放失败' }))
      setVoicePreviewState(current => nextPreviewState(current, { type: 'ready', voiceId: voice.id, token, url: result.url }))
      await audio.play()
    } catch (error) {
      setVoicePreviewState(current => nextPreviewState(current, { type: 'error', voiceId: voice.id, token, error: errorToastMessage(error) }))
    }
  }

  const saveWorkspaceSettings = async patch => {
    setBusyAction('settings')
    try {
      const result = await updateTaskWorkspaceSettings(taskId, {
        ...patch,
        expected_plan_version: workspaceRef.current?.plan_version,
      })
      if (patch.voice_type) localStorage.setItem(LAST_VOICE_KEY, patch.voice_type)
      if (patch.tts_options) localStorage.setItem(LAST_TTS_OPTIONS_KEY, JSON.stringify(patch.tts_options))
      setWorkspace(current => {
        const next = {
          ...current,
          ...patch,
          plan_version: result.plan_version,
          snapshot_key: result.snapshot_key,
        }
        workspaceRef.current = next
        return next
      })
      workspacePolling.refresh()
      return true
    } catch (error) {
      toast.error(errorToastMessage(error))
      if (error?.response?.status === 409) workspacePolling.refresh()
      return false
    } finally {
      setBusyAction('')
    }
  }

  const confirmVoice = async () => {
    if (!selectedVoice) return toast.warning('请先选择一个配音音色')
    setBusyAction('voice-validation')
    try {
      await previewVoice(
        { voice_type: selectedVoice, tts_options: ttsOptions },
        { silent: true },
      )
    } catch (error) {
      setBusyAction('')
      return toast.error(`该音色当前无法生成配音：${errorToastMessage(error)}`)
    }
    const saved = await saveWorkspaceSettings({ voice_type: selectedVoice, tts_options: ttsOptions, voice_confirmed: true })
    if (saved) {
      setWorkspaceSettingsOpen(false)
      if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('preview')
      toast.success('全片音色已确认')
    }
  }

  const saveRuntimeConfig = async () => {
    const normalized = normalizeRuntimeConfig(runtimeConfig)
    setRuntimeConfig(normalized)
    try {
      const taskSaved = await saveWorkspaceSettings({
        generation_options: normalized,
        voice_confirmed: voiceReady,
      })
      if (!taskSaved) return
      setBusyAction('runtime')
      const saved = await updateConfig({ generation: normalized })
      const next = normalizeRuntimeConfig(saved?.generation || normalized)
      setRuntimeConfig(next)
      localStorage.setItem(LAST_RUNTIME_KEY, JSON.stringify(next))
      toast.success('项目生成策略已保存，并设为后续项目默认值')
    } catch (error) {
      toast.error(errorToastMessage(error))
    } finally {
      setBusyAction('')
    }
  }

  const startAssets = async () => {
    if (savingCount || Object.keys(pendingRef.current).length) return toast.info('请等待当前编辑保存完成')
    setBusyAction('generate')
    try {
      await generateTaskWorkspaceAssets(taskId, { snapshot_key: workspace.snapshot_key })
      toast.success('已开始生成图片与配音')
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
      if (error?.response?.status === 409) workspacePolling.refresh()
    } finally {
      setBusyAction('')
    }
  }

  const regenerateOne = async (segment, target) => {
    if (savingCount || Object.keys(pendingRef.current).length) {
      return toast.info('请等待当前分镜设置保存完成')
    }
    setBusyAction(`${target}:${segment.segment_index}`)
    try {
      await retryTaskAssets(taskId, {
        snapshot_key: workspace.snapshot_key,
        scope: 'selected',
        targets: [{ segment_index: segment.segment_index, asset_type: target }],
      })
      workspacePolling.refresh()
      toast.success(target === 'image' ? '已开始处理这一张图片' : '已开始处理这一段配音')
    } catch (error) {
      toast.error(errorToastMessage(error))
    } finally {
      setBusyAction('')
    }
  }

  const regeneratePrompt = async segment => {
    const actionKey = `prompt:${segment.segment_index}`
    setBusyAction(actionKey)
    try {
      await regenerateSegmentPrompt(taskId, segment.segment_index, { snapshot_key: workspaceRef.current?.snapshot_key })
      toast.success(`已开始重新生成第 ${segment.segment_index + 1} 段提示词`)
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
      if (error?.response?.status === 409) workspacePolling.refresh()
    } finally {
      setBusyAction('')
    }
  }

  const applyServerSegmentPatch = (segmentIndex, patch) => {
    setWorkspace(current => {
      if (!current) return current
      const next = {
        ...current,
        segments: current.segments.map(segment => (
          Number(segment.segment_index) === Number(segmentIndex) ? { ...segment, ...patch } : segment
        )),
      }
      workspaceRef.current = next
      return next
    })
  }

  const loadImageHistory = async (segmentIndex, { force = false } = {}) => {
    if (!force && imageHistories[segmentIndex]) return
    const expectedTaskId = taskId
    setHistoryLoading(true)
    try {
      const assets = await getTaskAssets(expectedTaskId, { type: 'image', segment_index: segmentIndex })
      if (activeTaskIdRef.current !== expectedTaskId) return
      setImageHistories(current => ({ ...current, [segmentIndex]: Array.isArray(assets) ? assets : [] }))
    } catch (error) {
      if (activeTaskIdRef.current === expectedTaskId) toast.error(errorToastMessage(error))
    } finally {
      if (activeTaskIdRef.current === expectedTaskId) setHistoryLoading(false)
    }
  }

  const toggleImageHistory = segment => {
    const segmentIndex = segment.segment_index
    if (historySegmentIndex === segmentIndex) {
      setHistorySegmentIndex(null)
      return
    }
    setHistorySegmentIndex(segmentIndex)
    void loadImageHistory(segmentIndex)
  }

  const chooseHistoryVersion = async version => {
    const segment = workspaceRef.current?.segments?.[selectedIndex]
    const asset = version?.raw
    if (!segment || !asset?.asset_id || asset.asset_id === selectedHistoryId) return
    const actionKey = `history:${segment.segment_index}`
    setBusyAction(actionKey)
    try {
      const result = await selectSegmentImage(taskId, segment.segment_index, asset.asset_id)
      applyServerSegmentPatch(segment.segment_index, {
        image_path: result.image_path,
        image_url: result.image_url,
        image_prompt: result.image_prompt,
        image_status: 'completed',
        image_error: null,
        image_error_code: null,
        image_error_meta: null,
      })
      toast.success('已切换为这个图片版本')
      await loadImageHistory(segment.segment_index, { force: true })
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
    } finally {
      setBusyAction('')
    }
  }

  const requestImageUpload = segment => {
    uploadSegmentRef.current = segment.segment_index
    uploadInputRef.current?.click()
  }

  const handleImageUpload = async event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    const segmentIndex = uploadSegmentRef.current
    uploadSegmentRef.current = null
    if (!file || segmentIndex === null || segmentIndex === undefined) return
    const actionKey = `upload:${segmentIndex}`
    setBusyAction(actionKey)
    try {
      const result = await uploadImage(taskId, segmentIndex, file)
      applyServerSegmentPatch(segmentIndex, {
        image_path: result.image_path,
        image_url: result.image_url,
        image_status: 'completed',
        image_error: null,
        image_error_code: null,
        image_error_meta: null,
      })
      toast.success('替换图片已保存，旧版本仍保留在历史中')
      if (historySegmentIndex === segmentIndex) await loadImageHistory(segmentIndex, { force: true })
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
    } finally {
      setBusyAction('')
    }
  }

  const openLightboxForSegment = segment => {
    const index = lightboxItems.findIndex(item => Number(item.segment.segment_index) === Number(segment.segment_index))
    if (index < 0) return
    setLightboxIndex(index)
    setLightboxOpen(true)
  }

  const resegment = () => setResegmentConfirmOpen(true)

  const confirmResegment = async () => {
    if (busyAction === 'resegment') return
    setBusyAction('resegment')
    try {
      await resegmentTaskWorkspace(taskId, {
        script_text: workspace.script_text,
        expected_plan_version: workspace.plan_version,
      })
      setResegmentConfirmOpen(false)
      toast.success('已开始重新拆分分镜')
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
    } finally {
      setBusyAction('')
    }
  }

  const closeWorkspaceTour = () => {
    try { localStorage.setItem(WORKSPACE_TOUR_KEY, 'seen') } catch { /* localStorage 不可用时不阻断制作 */ }
    setTourOpen(false)
  }

  const createFullVideoPreview = async () => {
    if (previewJobIsActive) {
      setPreviewMode('full')
      if (previewJobPolling.error) previewJobPolling.reconnect()
      return
    }
    setBusyAction('preview')
    setPreviewMode('full')
    const expectedTaskId = taskId
    try {
      const forceRender = Boolean(exportState?.preview?.valid)
      const job = await createExport(expectedTaskId, { target: 'mp4', use_preview: !forceRender, auto_download: false })
      if (activeTaskIdRef.current !== expectedTaskId) return
      setPreviewJob(job)
      if (job.status === 'completed') {
        const state = await getExportState(expectedTaskId, { silent: true })
        if (activeTaskIdRef.current === expectedTaskId) setExportState(state)
        setPreviewMode('full')
      } else toast.success('已开始生成完整视频预览')
    } catch (error) {
      if (activeTaskIdRef.current !== expectedTaskId) return
      setPreviewMode(fullVideoUrl ? 'full' : 'content')
      toast.error(errorToastMessage(error))
    } finally {
      if (activeTaskIdRef.current === expectedTaskId) setBusyAction('')
    }
  }

  const cancelFullVideoPreview = async () => {
    if (!previewJobIsActive || previewJob?.cancel_requested) return
    setBusyAction('cancel-preview')
    try {
      const nextJob = await cancelExportJob(taskId, previewJob.job_id)
      if (activeTaskIdRef.current !== taskId) return
      setPreviewJob(nextJob)
      toast.info('已请求取消；正在安全收尾已开始的片段')
    } catch (error) {
      if (activeTaskIdRef.current === taskId) toast.error(errorToastMessage(error))
    } finally {
      if (activeTaskIdRef.current === taskId) setBusyAction('')
    }
  }

  const resumeGeneration = async () => {
    setBusyAction('resume')
    try {
      const recoveryAction = recoveryActionForWorkspace(workspace)
      if (recoveryAction === 'retry_assets') {
        await retryTaskAssets(taskId, {
          snapshot_key: workspace.snapshot_key,
          scope: 'failed',
        })
        toast.success(`已开始修复 ${workspace.recovery?.targets?.length || 0} 个失败素材`)
      } else if (recoveryAction === 'update_stale_assets') {
        const targets = workspace.recovery?.targets || []
        await retryTaskAssets(taskId, {
          snapshot_key: workspace.snapshot_key,
          scope: 'selected',
          targets,
        })
        toast.success(`已开始更新 ${targets.length} 个受影响素材`)
      } else if (recoveryAction === 'finalize') {
        await finalizeTaskWorkspace(taskId, { snapshot_key: workspace.snapshot_key })
        toast.success('已开始使用现有素材完成生产')
      } else {
        await resumeTask(taskId)
        toast.success('已从预案检查点继续生成')
      }
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
      workspacePolling.refresh()
    } finally {
      setBusyAction('')
    }
  }

  const retryAllFailed = async () => {
    const assetFailureCount = workspaceIssues.counts.image + workspaceIssues.counts.audio
    if (!assetFailureCount) return toast.info('提示词失败需逐段精确重新生成')
    setBusyAction('retry-failed')
    try {
      await retryTaskAssets(taskId, buildFailedRetryPayload(workspace))
      toast.success(`已开始重试 ${assetFailureCount} 个失败素材`)
      workspacePolling.refresh()
    } catch (error) {
      toast.error(errorToastMessage(error))
      workspacePolling.refresh()
    } finally {
      setBusyAction('')
    }
  }

  const retryFailedPrompt = segmentIndex => {
    const segment = segments.find(item => Number(item.segment_index) === Number(segmentIndex))
    if (segment) void regeneratePrompt(segment)
  }

  const locateFailedSegment = segmentIndex => {
    const position = segments.findIndex(segment => Number(segment.segment_index) === Number(segmentIndex))
    if (position < 0) return
    selectSegment(position)
    setWorkspaceSettingsOpen(false)
    if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('storyboard')
    window.requestAnimationFrame(() => {
      const element = document.querySelector(`.workspace-content [data-workspace-segment="${segmentIndex}"]`)
      element?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      element?.focus?.({ preventScroll: true })
    })
  }

  if (loading) return <main className="workspace-loading"><BrandLoader label="正在恢复生产工作台" /></main>
  if (loadError || !workspace) return <main className="workspace-loading"><div className="workspace-error-card" role="alert"><CircleAlert size={24} /><strong>{missingTask ? '项目已不存在' : '工作台暂时无法打开'}</strong><p>{loadError}</p><div>{!missingTask ? <button type="button" className="button button-secondary" onClick={() => { setLoading(true); setLoadError(''); workspacePolling.reconnect() }}>重新连接</button> : null}<button type="button" className="button button-primary" onClick={() => navigate('/assets')}>返回项目资产</button></div></div></main>

  const closeSettingsPanel = () => {
    if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('storyboard')
    else setWorkspaceSettingsOpen(false)
  }

  const navigateJourneyStep = (_step, index) => {
    if (index === 0) {
      navigate(workspace.source_draft_id ? `/manuscript/${workspace.source_draft_id}` : '/manuscript')
      return
    }
    if (index === 1) {
      previewScrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
      return
    }
    if (index === 2) {
      setWorkspaceSettingsOpen(true)
      if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('settings')
      return
    }
    if (index === 3) {
      setWorkspaceSettingsOpen(false)
      if (window.matchMedia?.('(max-width: 780px)').matches) selectMobilePane('storyboard')
      return
    }
    if (index === 4) {
      const target = segments.findIndex(segment => ['pending', 'processing', 'failed', 'stale'].includes(segment.image_status) || ['pending', 'processing', 'failed', 'stale'].includes(segment.audio_status))
      if (target >= 0) selectSegment(target)
      return
    }
    if (index === 5) navigate(`/export/${taskId}`)
  }

  return <main className={`production-workspace${settingsOpen ? ' is-settings-open' : ''}`} data-mobile-pane={mobilePane}>
    <WorkspaceStageNavigator journey={journey} onHelp={() => setTourOpen(true)} onNavigate={navigateJourneyStep} />
    <div className="workspace-polling-notices">
      {workspacePolling.error ? <PollingFailureNotice
        title="工作台连接已中断"
        description="当前画面、选中分镜和未同步编辑均已保留；恢复连接后会继续刷新。"
        onReconnect={workspacePolling.reconnect}
        reconnecting={workspacePolling.inFlight}
      /> : null}
      {previewJobPolling.error && previewJobIsActive ? <PollingFailureNotice
        title="完整视频进度暂时无法查询"
        description="生成编号已保留。重新连接只会查询同一次生成，不会重复创建视频。"
        onReconnect={() => { setPreviewMode('full'); previewJobPolling.reconnect() }}
        reconnecting={previewJobPolling.inFlight}
      /> : null}
    </div>
    <nav className="workspace-mobile-tabs" aria-label="工作台分区">
      <button type="button" aria-pressed={mobilePane === 'storyboard'} className={mobilePane === 'storyboard' ? 'is-active' : ''} onClick={() => selectMobilePane('storyboard')}>分镜</button>
      <button type="button" aria-pressed={mobilePane === 'preview'} className={mobilePane === 'preview' ? 'is-active' : ''} onClick={() => selectMobilePane('preview')}>预览</button>
      <button type="button" aria-pressed={mobilePane === 'settings'} className={mobilePane === 'settings' ? 'is-active' : ''} onClick={() => { setWorkspaceSettingsOpen(true); selectMobilePane('settings') }}>设置</button>
    </nav>
    <div className="workspace-grid">
      <aside ref={previewScrollRef} className="workspace-preview" aria-label="当前分镜预览" onScroll={event => rememberPaneScroll('preview', event)}>
        <header className="workspace-preview-heading">
          <div><span>内容预览</span><strong>{selectedIndex + 1} / {segments.length || 0}</strong></div>
          <span className={`workspace-stage-dot is-${stage.tone}`}>{stage.title}</span>
        </header>

        <section className={`workspace-canvas ${ratioClassName(workspace.ratio)}`}>
          {previewMode === 'full' && isRenderingFullVideo
            ? <div className="workspace-full-video-rendering" role="status" aria-live="polite">
                <span className="workspace-render-orbit"><i /></span>
                <strong>正在生成完整视频预览</strong>
                <small>{previewJob?.message || '正在合成画面、字幕与配音，请稍候'}</small>
              </div>
            : previewMode === 'full' && fullVideoUrl
              ? <video
                  ref={fullVideoRef}
                  controls
                  preload="metadata"
                  src={fullVideoUrl}
                  aria-label="完整视频预览"
                  onPlay={() => setPlaying(true)}
                  onPause={() => setPlaying(false)}
                  onEnded={event => { event.currentTarget.currentTime = 0; setPlaying(false) }}
                />
            : imageUrl
              ? <img src={imageUrl} alt={`分镜 ${selectedIndex + 1} 画面`} />
              : <div className="workspace-image-pending" style={{ backgroundImage: `url(${activeStyle?.image || ''})` }}>
                  <span className="workspace-image-glow" />
                  <ImageOff size={24} />
                  <strong>{currentSegment?.image_prompt ? '画面等待生成' : '正在准备画面描述'}</strong>
                  <small>{workspace.visual_style} · {workspace.ratio}</small>
                </div>}
          {previewMode === 'content' ? <p className={`workspace-subtitle is-size-${workspace.subtitle_options?.size || 'standard'} is-position-${workspace.subtitle_options?.position || 'standard'} is-outline-${workspace.subtitle_options?.outline || 'standard'}`}>{normalizeSubtitleText(currentSegment?.text || '分镜文案生成后会显示在这里')}</p> : null}
        </section>

        <div className="workspace-player-controls">
          <button type="button" className="workspace-icon-button" onClick={() => selectSegment(selectedIndex - 1)} disabled={previewMode === 'full' || selectedIndex <= 0} aria-label="上一段"><ChevronLeft size={17} /></button>
          <button type="button" className="workspace-play-button" onClick={togglePlayback} disabled={previewMode === 'full' ? !fullVideoUrl || isRenderingFullVideo : !segments.length} aria-label={playing ? `暂停${previewMode === 'full' ? '完整视频' : '即时预览'}` : `播放${previewMode === 'full' ? '完整视频' : '即时预览'}`}>{playing ? <Pause size={18} /> : <Play size={18} />}</button>
          <button type="button" className="workspace-icon-button" onClick={() => selectSegment(selectedIndex + 1)} disabled={previewMode === 'full' || selectedIndex >= segments.length - 1} aria-label="下一段"><ChevronRight size={17} /></button>
          <div><strong>{secondsToLabel(segments.slice(0, selectedIndex).reduce((sum, segment) => sum + segmentDuration(segment), 0))}</strong><span>/ {secondsToLabel(workspace.estimated_duration)}</span></div>
        </div>

        <div className="workspace-preview-tabs">
          <button type="button" aria-pressed={previewMode === 'content'} className={previewMode === 'content' ? 'is-active' : ''} onClick={() => changePreviewMode('content')}>分段即时预览</button>
          <button type="button" aria-pressed={previewMode === 'full'} className={previewMode === 'full' ? 'is-active' : ''} disabled={!fullVideoUrl && !isRenderingFullVideo} onClick={() => changePreviewMode('full')}>完整视频预览</button>
        </div>

        <dl className="workspace-metrics">
          <div><dt>实际分镜</dt><dd>{workspace.segments_count} 段</dd></div>
          <div><dt>{workspace.duration_is_estimate ? '预计时长' : '真实时长'}</dt><dd>{secondsToLabel(workspace.estimated_duration)}</dd></div>
          <div><dt>素材耗时</dt><dd>{formatEstimate(workspace.generation_estimate)}</dd></div>
        </dl>

        <section className={`workspace-stage-note is-${stage.tone}`}>
          {stage.tone === 'warning' ? <AlertTriangle size={17} /> : stage.tone === 'working' ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
          <div><strong>{stage.title}</strong><p>{stage.description}</p></div>
        </section>
        <WorkspaceFailureBanner
          issues={workspaceIssues}
          busy={Boolean(busyAction) || operationRunning}
          onRetryAll={retryAllFailed}
          onRetryPrompt={retryFailedPrompt}
          onSelect={locateFailedSegment}
        />
        {workspace.active_operation ? <section className="workspace-operation-progress" aria-live="polite">
          <div><strong>{workspace.active_operation.kind === 'finalize' ? '生产草稿' : '素材更新'}</strong><span>{workspace.active_operation.completed}/{workspace.active_operation.total}</span></div>
          <progress value={workspace.active_operation.completed} max={Math.max(1, workspace.active_operation.total)} aria-label={`当前操作进度 ${workspace.active_operation.completed}/${workspace.active_operation.total}`} />
          <small>{workspace.active_operation.failed ? `${workspace.active_operation.failed} 项失败，其他项目继续处理` : '离开本页不会中断当前操作'}</small>
        </section> : null}

        <details className={`workspace-center-script${workspace.script_text ? '' : ' is-loading'}`} open>
          <summary><span><strong>完整文案</strong><small>{workspace.script_source === 'reconstructed_segments' ? '由已保存分镜恢复' : workspace.input_mode === 'theme' ? '主题扩写结果' : '严格保留用户原文'}</small></span><span>{workspace.script_text ? `${workspace.script_text.replace(/\s/g, '').length} 字${workspace.script_source === 'reconstructed_segments' ? ' · 恢复稿' : ''}` : '生成中'}</span></summary>
          {workspace.script_text ? <p>{workspace.script_text}</p> : <div className="workspace-script-skeleton"><i /><i /><i /><i /></div>}
        </details>

        <section className="workspace-preview-table-panel">
          <header><div><strong>分镜总表</strong><span>{workspace.progress.prompts_ready}/{workspace.progress.prompts_total || workspace.segments_count} 提示词完成{workspace.progress.prompts_failed ? ` · ${workspace.progress.prompts_failed} 段失败` : ''}</span></div><button type="button" onClick={resegment} disabled={!editable || busyAction === 'resegment'}><RefreshCw size={13} />重新拆分</button></header>
          <div className="workspace-preview-table">
            <div className="workspace-preview-row is-head"><span>#</span><span>文案</span><span>时长</span><span>提示词</span><span>图片</span><span>配音</span></div>
            {!segments.length ? Array.from({ length: 4 }, (_, index) => <div className="workspace-preview-row is-skeleton" key={index}><i /><i /><i /><i /><i /><i /></div>) : segments.map((segment, index) => <button type="button" key={segment.id || segment.segment_index} aria-pressed={index === selectedIndex} className={`workspace-preview-row${index === selectedIndex ? ' is-selected' : ''}`} onClick={() => selectSegment(index)}>
              <strong>{String(index + 1).padStart(2, '0')}</strong>
              <span>{normalizeSubtitleText(segment.text || '等待文案')}</span>
              <span><Clock3 size={12} />{secondsToLabel(segmentDuration(segment))}</span>
              <span className={`is-${segment.prompt_status}`}>{assetStatusLabel(segment.prompt_status)}</span>
              <span className={`is-${segment.image_status}`}>{assetStatusLabel(segment.image_status)}</span>
              <span className={`is-${segment.audio_status}`}>{assetStatusLabel(segment.audio_status)}</span>
            </button>)}
          </div>
        </section>
      </aside>

      <WorkspaceStoryboardNav
        scrollRef={storyboardScrollRef}
        onScroll={event => rememberPaneScroll('storyboard', event)}
        workspace={workspace}
        stage={stage}
        activeStyle={activeStyle}
        saveMessage={saveMessage}
        editable={editable}
        busyAction={busyAction}
        segments={segments}
        selectedIndex={selectedIndex}
        onSelect={selectSegment}
        onResegment={resegment}
        getSegmentState={segmentState}
      />

      <WorkspaceInspector
        taskId={taskId}
        segmentScrollRef={segmentInspectorScrollRef}
        settingsScrollRef={fullSettingsScrollRef}
        onSegmentScroll={event => rememberPaneScroll('segmentInspector', event)}
        onSettingsScroll={event => rememberPaneScroll('fullSettings', event)}
        open={settingsOpen}
        onToggle={() => setWorkspaceSettingsOpen(open => !open)}
        onClose={closeSettingsPanel}
        currentSegment={currentSegment}
        currentState={segmentState(currentSegment)}
        selectedIndex={selectedIndex}
        segmentCount={segments.length}
        onSelectSegment={selectSegment}
        editable={editable}
        onSegmentChange={enqueueSave}
        voices={voices}
        workspace={workspace}
        operationTarget={operationTarget}
        busyAction={busyAction}
        onRegenerate={regenerateOne}
        onRegeneratePrompt={regeneratePrompt}
        imageUrl={imageUrl}
        imageHistoryOpen={Boolean(currentSegment && historySegmentIndex === currentSegment.segment_index)}
        imageHistoryVersions={currentHistoryVersions}
        selectedHistoryId={selectedHistoryId}
        historyLoading={historyLoading}
        onOpenImage={() => currentSegment && openLightboxForSegment(currentSegment)}
        onUploadImage={() => currentSegment && requestImageUpload(currentSegment)}
        onToggleImageHistory={() => currentSegment && toggleImageHistory(currentSegment)}
        onSelectHistoryVersion={chooseHistoryVersion}
        storageWarning={Boolean(currentSegment?.image_storage_warning || currentSegment?.audio_storage_warning)}
        selectedVoice={selectedVoice}
        ttsOptions={ttsOptions}
        onVoiceChange={id => { stopVoicePreview(); setSelectedVoice(id); setTtsOptions(mergeTtsOptions({}, workspace.tts_options || {}, id.startsWith('doubao:') ? 'doubao' : 'mimo')) }}
        onTtsOptionsChange={options => { stopVoicePreview(); setTtsOptions(options) }}
        onVoicePreview={previewSelectedVoice}
        onStopVoicePreview={stopVoicePreview}
        voicePreviewState={voicePreviewState}
        onConfirmVoice={confirmVoice}
        voiceReady={voiceReady}
        visualStyles={visualStyles}
        onSaveSettings={saveWorkspaceSettings}
        ratioOptions={ratioOptions}
        runtimeConfig={runtimeConfig}
        setRuntimeConfig={setRuntimeConfig}
        normalizeRuntime={normalizeRuntimeConfig}
        onSaveRuntime={saveRuntimeConfig}
        onOpenApi={() => navigate(`/workspace/${taskId}/settings`)}
        onAssetSelected={({ assetType }) => {
          toast.success(assetType === 'audio' ? '已切换为这个配音版本' : '已切换为这个图片版本')
          workspacePolling.refresh()
        }}
        pendingEdits={Boolean(savingCount || Object.keys(pendingRef.current).length)}
      />
    </div>

    <WorkspaceActionBar
      workspace={workspace}
      stage={stage}
      voiceReady={voiceReady}
      voiceLabel={voiceName(voices, workspace.voice_type)}
      visualPlanReady={visualPlanReady}
      savingCount={savingCount}
      busyAction={busyAction}
      recoverable={isRecoverableStage}
      canResume={canResume}
      recoverySummary={recoverySummary}
      recoveryActionLabel={recoveryActionLabel}
      canRenderFullVideo={canRenderFullVideo}
      renderingFullVideo={isRenderingFullVideo}
      fullVideoJobActive={previewJobIsActive}
      cancellingFullVideo={Boolean(previewJob?.cancel_requested || busyAction === 'cancel-preview')}
      previewPollingFailed={Boolean(previewJobPolling.error)}
      previewValid={Boolean(exportState?.preview?.valid)}
      canEnterExport={canEnterExport}
      onResume={resumeGeneration}
      onConfirmVoice={confirmVoice}
      onGenerateAssets={startAssets}
      onFullVideo={createFullVideoPreview}
      onCancelFullVideo={cancelFullVideoPreview}
      onExport={() => navigate(`/export/${taskId}`)}
    />

      <input ref={uploadInputRef} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={handleImageUpload} />
      <Lightbox
        open={lightboxOpen}
        items={lightboxItems}
        activeIndex={lightboxIndex}
        onIndexChange={(nextIndex, item) => {
          setLightboxIndex(nextIndex)
          if (item) selectSegment(item.segmentPosition)
        }}
        onClose={() => setLightboxOpen(false)}
        promptSlot={item => item?.prompt ? <><strong>画面提示词</strong><span>{item.prompt}</span></> : <span>这个分镜还没有画面提示词。</span>}
        actionSlot={item => item?.segment ? <>
          <button type="button" className="button button-secondary" disabled={!editable || Boolean(busyAction)} onClick={() => requestImageUpload(item.segment)}><Upload size={14} />上传替换</button>
          <button type="button" className="button button-primary" disabled={!editable || Boolean(busyAction)} onClick={() => regenerateOne(item.segment, 'image')}><WandSparkles size={14} />重新生成</button>
        </> : null}
      />

      <WorkspaceSettingsOverlay open={isSettingsOverlay} onClose={() => navigate(`/workspace/${taskId}`)}>
        <SettingsPage embedded onClose={() => navigate(`/workspace/${taskId}`)} />
      </WorkspaceSettingsOverlay>
      <ConfirmDialog
        open={resegmentConfirmOpen}
        title="重新拆分全部分镜"
        message="重新拆分会重建分镜结构和自动提示词。当前手工编辑不会被静默映射到新分镜；已生成素材仍会保留在项目资产中，供你查看和回选。"
        confirmLabel={busyAction === 'resegment' ? '正在重新拆分…' : '确认重新拆分'}
        confirmDisabled={busyAction === 'resegment'}
        danger
        onConfirm={confirmResegment}
        onClose={() => { if (busyAction !== 'resegment') setResegmentConfirmOpen(false) }}
      />
      <Modal
        open={tourOpen}
        title="一体化工作台怎么用"
        onClose={closeWorkspaceTour}
        footer={<><button type="button" className="button button-secondary" onClick={closeWorkspaceTour}>跳过引导</button><button type="button" className="button button-primary" data-modal-initial-focus onClick={closeWorkspaceTour}>开始制作</button></>}
      >
        <ol className="workspace-tour-steps">
          <li><b>1</b><span><strong>看预案</strong><small>左侧按顺序列出分镜，中间展示完整文案、画面和素材状态。</small></span></li>
          <li><b>2</b><span><strong>先确认音色和画面</strong><small>右侧设置保存后，底部主按钮才会开始生成图片与配音。</small></span></li>
          <li><b>3</b><span><strong>逐段检查，再按需导出</strong><small>中间可以连续即时预览；完整视频只在你主动点击后生成。</small></span></li>
        </ol>
      </Modal>
    </main>
}

function assetStatusLabel(status) {
  return ({ completed: '完成', stale: '待更新', failed: '失败', processing: '生成中', pending: '等待' })[status] || '等待'
}

function voiceName(voices, id) {
  return voices.find(voice => voice.id === id)?.name || '尚未确认'
}
