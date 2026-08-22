import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  CheckCircle2,
  CircleAlert,
  Download,
  FileArchive,
  Film,
  FolderOpen,
  HardDriveDownload,
  LoaderCircle,
  PackageOpen,
  Square,
} from 'lucide-react'
import { useNavigate, useParams } from 'react-router'
import { cancelExportJob, createExport, getExportJob, getExportState, getMaterialsDownloadUrl, selectDraftFolder } from '../api/task'
import { PollingFailureNotice } from '../components/PollingFailureNotice'
import { ProjectStepBar } from '../components/ProjectStepBar'
import { EmptyState, LoadingState } from '../components/StatusStates'
import { usePollingResource } from '../hooks/usePollingResource'
import { detectTargetOS, validateExtractPath } from '../lib/exportPath'
import { toast } from '../lib/toast'
import { materialPackageSummary, resolveApiDownloadUrl } from './exportMaterials'
import { buildExportPollingKey, isActiveExportJob } from './exportPolling'
import './delivery-pages.css'

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:2002'

function isBusy(job) {
  return isActiveExportJob(job)
}

function jobLabel(job) {
  if (!job) return ''
  if (job.cancel_requested && isBusy(job)) return '正在取消'
  if (job.status === 'cancelled') return '已取消'
  if (job.status === 'completed') return '已完成'
  if (job.status === 'failed') return '失败'
  if (job.status === 'processing') return '处理中'
  if (job.status === 'pending') return '等待中'
  return job.status || ''
}

function triggerFileDownload(value) {
  const url = resolveApiDownloadUrl(API_BASE, value)
  if (!url) return false
  const link = document.createElement('a')
  link.href = url
  link.rel = 'noopener'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  link.remove()
  return true
}

export function ExportPage() {
  const { taskId } = useParams()
  const navigate = useNavigate()
  const notifiedJobs = useRef(new Set())
  const downloadedMp4Jobs = useRef(new Set())
  const downloadedMaterialJobs = useRef(new Set())
  const activeTaskIdRef = useRef(taskId)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [loadedTaskId, setLoadedTaskId] = useState(null)
  const [state, setState] = useState(null)
  const [jobs, setJobs] = useState({ mp4: null, materials: null, draft: null, draft_local: null })
  const [extractPath, setExtractPath] = useState(() => (
    localStorage.getItem('kepu:mine:extract_path') || localStorage.getItem('extract_path') || ''
  ))
  const [targetOS, setTargetOSState] = useState(() => (
    localStorage.getItem('kepu:mine:draft_target_os') || detectTargetOS()
  ))
  const [defaultExport] = useState(() => localStorage.getItem('kepu:mine:default_export') || 'mp4')
  const [folderPicking, setFolderPicking] = useState(false)
  const pathCheck = useMemo(() => validateExtractPath(extractPath, targetOS), [extractPath, targetOS])
  const activeJobsKey = useMemo(
    () => loadedTaskId === taskId ? buildExportPollingKey(taskId, jobs) : null,
    [jobs, loadedTaskId, taskId],
  )

  activeTaskIdRef.current = taskId

  const applyExportState = useCallback((nextState, expectedTaskId, { replaceJobs = false } = {}) => {
    if (activeTaskIdRef.current !== expectedTaskId) return false
    const stateJobs = Array.isArray(nextState?.jobs) ? nextState.jobs : []
    setState(nextState)
    setLoadedTaskId(expectedTaskId)
    setJobs(current => {
      const fallback = replaceJobs
        ? { mp4: null, materials: null, draft: null, draft_local: null }
        : current
      return {
        mp4: stateJobs.find(job => job.target === 'mp4') || fallback.mp4,
        materials: stateJobs.find(job => job.target === 'materials') || fallback.materials,
        draft: stateJobs.find(job => job.target === 'draft') || fallback.draft,
        draft_local: stateJobs.find(job => job.target === 'draft_local') || fallback.draft_local,
      }
    })
    setLoadError('')
    return true
  }, [])

  const loadState = useCallback(async ({ showLoader = false, silent = false } = {}) => {
    const expectedTaskId = taskId
    if (showLoader) setLoading(true)
    try {
      const nextState = await getExportState(expectedTaskId, { silent: true })
      applyExportState(nextState, expectedTaskId)
    } catch (error) {
      if (activeTaskIdRef.current !== expectedTaskId) return
      console.error('加载导出状态失败', error)
      // 后台对账失败时保留已加载的导出状态，不用全屏错误覆盖现有数据。
      if (!silent) {
        setLoadError('导出状态不可用，请确认后端服务在线后重试。')
        toast.error('加载导出状态失败')
      }
    } finally {
      if (showLoader && activeTaskIdRef.current === expectedTaskId) setLoading(false)
    }
  }, [applyExportState, taskId])

  useEffect(() => {
    let active = true
    setLoading(true)
    setLoadError('')
    setState(null)
    setLoadedTaskId(null)
    setJobs({ mp4: null, materials: null, draft: null, draft_local: null })
    getExportState(taskId, { silent: true })
      .then(nextState => {
        if (!active) return
        applyExportState(nextState, taskId, { replaceJobs: true })
      })
      .catch(error => {
        if (!active) return
        console.error('加载导出状态失败', error)
        setLoadError('导出状态不可用，请确认后端服务在线后重试。')
        toast.error('加载导出状态失败')
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [applyExportState, taskId])

  const exportJobsPolling = usePollingResource({
    resourceKey: activeJobsKey,
    enabled: Boolean(activeJobsKey),
    interval: 2000,
    request: async (key, { signal }) => {
      const batch = JSON.parse(key)
      const results = await Promise.all(batch.jobs.map(async ({ target, jobId }) => {
        try {
          return [target, await getExportJob(batch.taskId, jobId, { silent: true, signal }), false]
        } catch (error) {
          if (error?.response?.status === 404) return [target, null, true]
          throw error
        }
      }))
      return { taskId: batch.taskId, results }
    },
    onData: ({ taskId: polledTaskId, results }) => {
      if (activeTaskIdRef.current !== polledTaskId) return
      let shouldReconcile = false
      let finished = false
      results.forEach(([target, job, missing]) => {
        if (missing) {
          shouldReconcile = true
          return
        }
        if (!job || isActiveExportJob(job)) return
        finished = true
        const notificationKey = `${job.job_id}:${job.status}`
        if (notifiedJobs.current.has(notificationKey)) return
        notifiedJobs.current.add(notificationKey)
        if (job.status === 'completed') {
          if (target === 'materials') {
            toast.success(job.result?.complete ? '分镜素材包已整理完成' : '部分素材包已整理完成')
            if (job.result?.download_url && !downloadedMaterialJobs.current.has(job.job_id)) {
              downloadedMaterialJobs.current.add(job.job_id)
              triggerFileDownload(job.result.download_url)
            }
          } else {
            toast.success(target === 'mp4' ? 'MP4 已生成' : target === 'draft_local' ? '已写入剪映草稿目录' : '草稿下载已准备好')
            if (target === 'mp4' && job.params?.auto_download !== false && !downloadedMp4Jobs.current.has(job.job_id)) {
              downloadedMp4Jobs.current.add(job.job_id)
              triggerFileDownload(`/ai/native/video/kepu/tasks/${polledTaskId}/download-mp4`)
            }
          }
        } else if (job.status === 'failed') {
          toast.error(job.error || '导出失败')
        } else if (job.status === 'cancelled') {
          toast.info('导出已取消，已有可用文件未被覆盖')
        }
      })
      setJobs(current => {
        const next = { ...current }
        results.forEach(([target, job]) => {
          if (job) next[target] = job
        })
        return next
      })
      if (finished || shouldReconcile) void loadState({ silent: true })
    },
  })

  const saveExtractPath = value => {
    setExtractPath(value)
    localStorage.setItem('kepu:mine:extract_path', value || '')
    if (value) localStorage.setItem('extract_path', value)
  }

  const setTargetOS = value => {
    const next = value === 'mac' ? 'mac' : 'windows'
    setTargetOSState(next)
    localStorage.setItem('kepu:mine:draft_target_os', next)
  }

  const chooseDraftFolder = async () => {
    setFolderPicking(true)
    toast.info('请在弹出的窗口里选择剪映草稿目录')
    try {
      const result = await selectDraftFolder(taskId)
      const nextPath = result?.path || ''
      const nextOS = result?.target_os || targetOS
      saveExtractPath(nextPath)
      setTargetOS(nextOS)
      if (result?.warnings?.length) toast.warning(result.warnings[0])
      else toast.success('已选择剪映草稿目录')
    } catch (error) {
      console.error('选择剪映草稿目录失败', error)
      toast.error(error?.response?.data?.detail || '未选择文件夹')
    } finally {
      setFolderPicking(false)
    }
  }

  const startExport = async (target, { forceRender = false } = {}) => {
    const expectedTaskId = taskId
    const payload = { target, use_preview: !forceRender }
    if (target === 'mp4') payload.auto_download = true
    if (target === 'draft_local') {
      if (!pathCheck.valid) {
        toast.warning(pathCheck.issues[0] || '请先选择剪映草稿目录')
        return
      }
      Object.assign(payload, {
        draft_root: pathCheck.normalized,
        target_os: targetOS,
        overwrite: true,
      })
    }
    try {
      const job = await createExport(expectedTaskId, payload)
      if (activeTaskIdRef.current !== expectedTaskId) return
      setJobs(current => ({ ...current, [target]: job }))
      toast.success(target === 'mp4' ? 'MP4 导出已开始' : target === 'materials' ? '正在按分镜顺序整理素材' : target === 'draft_local' ? '正在写入剪映草稿目录' : '草稿下载准备已开始')
    } catch (error) {
      if (activeTaskIdRef.current !== expectedTaskId) return
      console.error('创建导出任务失败', error)
      toast.error(error?.response?.data?.detail || '创建导出失败')
    }
  }

  const cancelJob = async target => {
    const job = jobs[target]
    if (!isBusy(job) || job.cancel_requested) return
    try {
      const nextJob = await cancelExportJob(taskId, job.job_id)
      if (activeTaskIdRef.current !== taskId) return
      setJobs(current => ({ ...current, [target]: nextJob }))
      toast.info('已请求取消；正在安全收尾已开始的片段')
    } catch (error) {
      if (activeTaskIdRef.current !== taskId) return
      toast.error(error?.response?.data?.detail || '取消导出失败')
    }
  }

  const downloadMp4 = () => {
    if (state?.outputs?.mp4?.available) window.open(`${API_BASE}/ai/native/video/kepu/tasks/${taskId}/download-mp4`, '_blank')
  }

  const downloadDraft = () => {
    const query = new URLSearchParams({ target_os: targetOS })
    if (pathCheck.valid && pathCheck.normalized) {
      saveExtractPath(pathCheck.normalized)
      query.set('extract_path', pathCheck.normalized)
    }
    localStorage.setItem('kepu:mine:draft_target_os', targetOS)
    window.open(`${API_BASE}/ai/native/video/kepu/tasks/${taskId}/download?${query.toString()}`, '_blank')
  }

  const downloadMaterials = () => {
    const materials = state?.outputs?.materials || {}
    const url = jobs.materials?.result?.download_url || materials.download_url || getMaterialsDownloadUrl(taskId, materials.snapshot_key)
    if (!triggerFileDownload(url)) toast.error('素材包下载地址不可用，请重新整理')
  }

  if (loading || (!loadError && loadedTaskId !== taskId)) return <main className="delivery-loading"><LoadingState label="正在读取导出状态..." /></main>
  if (loadError || !state || loadedTaskId !== taskId) {
    return <main className="delivery-loading"><EmptyState title="导出状态不可用" description={loadError} action={<button className="button button-primary" type="button" onClick={() => loadState({ showLoader: true })}>重试</button>} /></main>
  }

  const previewStatus = isBusy(jobs.mp4) ? '生成中' : !state.preview?.exists ? '未生成' : state.preview.valid ? '可播放' : state.preview.reason === 'stale' ? '已过期' : state.preview.reason === 'ratio_mismatch' ? '比例不一致' : '不可用'
  const draftStatus = jobs.draft_local?.status === 'completed' ? '已写入剪映' : isBusy(jobs.draft_local) ? '写入中' : state.outputs?.draft?.available ? '草稿可下载' : '未生成'
  const canvas = state.canvas || {}
  const mp4Available = Boolean(state.outputs?.mp4?.available)
  const draftAvailable = Boolean(state.outputs?.draft?.available)
  const canBuildRenderedOutputs = Boolean(state.outputs?.draft?.path)
  const materials = state.outputs?.materials || {}
  const materialsSummary = materialPackageSummary(materials)
  const materialDownloadReady = Boolean(materials.package_ready || (jobs.materials?.status === 'completed' && jobs.materials?.result?.download_url))

  return (
    <main className="delivery-page export-page">
      <ProjectStepBar taskId={taskId} currentStep={6} reachedStep={6} />
      <header className="delivery-heading">
        <button className="button button-secondary" type="button" onClick={() => navigate(`/preview/${taskId}`)}><ArrowLeft size={16} aria-hidden="true" />返回编辑</button>
        <div>
          <p className="eyebrow">导出中心</p>
          <h1>选择交付格式</h1>
          <p>生成成片、下载按分镜整理的素材包，或继续使用剪映草稿精修。</p>
        </div>
        <div className="export-canvas-meta"><span>{state.ratio || '--'}</span><strong>{canvas.width || '--'} × {canvas.height || '--'}</strong></div>
      </header>

      <section className="delivery-status-strip" aria-label="交付状态">
        <StatusMetric label="完整视频预览" value={previewStatus} ready={state.preview?.valid} warning={state.preview?.exists && !state.preview?.valid} />
        <StatusMetric label="MP4 成片" value={isBusy(jobs.mp4) ? '生成中' : mp4Available ? '可下载' : state.outputs?.mp4?.stale ? '已过期' : '未生成'} ready={mp4Available} warning={state.outputs?.mp4?.stale} />
        <StatusMetric label="分镜素材" value={materialsSummary.statusLabel} ready={materialsSummary.complete} warning={materialsSummary.available && !materialsSummary.complete} />
        <StatusMetric label="剪映草稿" value={draftStatus} ready={jobs.draft_local?.status === 'completed' || draftAvailable} warning={jobs.draft_local?.status === 'failed'} />
      </section>

      {exportJobsPolling.error ? <PollingFailureNotice
        title="导出连接中断"
        description="后台导出可能仍在继续；重新连接只会查询原记录，不会重复创建导出。"
        onReconnect={exportJobsPolling.reconnect}
        reconnecting={exportJobsPolling.inFlight}
      /> : null}

      <div className="export-options">
        <section className={`export-option${defaultExport === 'mp4' ? ' is-preferred' : ''}`}>
          <div className="export-option-title"><span><Film size={19} aria-hidden="true" /></span><div><h2>直接 MP4 视频</h2><p>{mp4Available ? '当前完整视频预览就是这份 MP4，下载不会重复渲染。' : state.outputs?.mp4?.stale ? '素材已变更，需要重新渲染当前版本。' : '生成一次后会自动下载，并同时成为完整视频预览。'}</p></div></div>
          <JobState job={jobs.mp4} fallback="尚未开始 MP4 导出" />
          <div className="export-actions">
            <button className="button button-primary" type="button" disabled={isBusy(jobs.mp4) || (!mp4Available && !canBuildRenderedOutputs)} onClick={mp4Available ? downloadMp4 : () => startExport('mp4')}>{isBusy(jobs.mp4) ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : mp4Available ? <Download size={16} aria-hidden="true" /> : <Film size={16} aria-hidden="true" />}{isBusy(jobs.mp4) ? '生成中...' : mp4Available ? '下载 MP4' : state.outputs?.mp4?.stale ? '重新生成并下载' : '生成并下载 MP4'}</button>
            {isBusy(jobs.mp4) ? <button className="button button-secondary" type="button" disabled={Boolean(jobs.mp4?.cancel_requested)} onClick={() => cancelJob('mp4')}><Square size={15} aria-hidden="true" />{jobs.mp4?.cancel_requested ? '正在取消…' : '取消生成'}</button> : null}
            {mp4Available ? <button className="button button-secondary" type="button" disabled={isBusy(jobs.mp4) || !canBuildRenderedOutputs} onClick={() => startExport('mp4', { forceRender: true })}><Film size={16} aria-hidden="true" />重新生成</button> : null}
          </div>
        </section>

        <section className="export-option export-option-materials">
          <div className="export-option-title"><span><PackageOpen size={19} aria-hidden="true" /></span><div><h2>分镜素材包</h2><p>按播放顺序整理当前使用的图片与逐段配音，解压后可直接用于其他剪辑软件。</p></div></div>
          <div className="export-material-summary" aria-label="素材包内容"><span><strong>{materials.image_count || 0}</strong>张图片</span><span><strong>{materials.audio_count || 0}</strong>段音频</span><span><strong>{materials.segment_count || 0}</strong>个分镜</span></div>
          {materialsSummary.warning ? <div className="delivery-message is-warning"><CircleAlert size={16} aria-hidden="true" /><div><span>{materialsSummary.warning}</span></div></div> : null}
          <JobState job={jobs.materials} fallback={materials.package_ready ? '素材包已准备好，可再次下载' : materialsSummary.available ? '将附带分镜清单、字幕和使用说明' : '暂无可打包素材'} />
          <div className="export-actions">
            <button className="button button-primary" type="button" disabled={!materialsSummary.available || isBusy(jobs.materials)} onClick={materialDownloadReady ? downloadMaterials : () => startExport('materials')}>{isBusy(jobs.materials) ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}{isBusy(jobs.materials) ? '正在整理...' : '下载素材包 ZIP'}</button>
          </div>
        </section>

        <section className={`export-option export-option-draft${defaultExport === 'draft' ? ' is-preferred' : ''}`}>
          <div className="export-option-title"><span><FileArchive size={19} aria-hidden="true" /></span><div><h2>剪映草稿</h2><p>推荐直接写入本机剪映草稿目录，也可以准备 ZIP 后下载。</p></div></div>
          <label className="delivery-field export-path-field">
            <span>剪映草稿目录</span>
            <div className="field-with-action">
              <input value={extractPath} onChange={event => saveExtractPath(event.target.value)} placeholder={targetOS === 'mac' ? '/Users/你的用户名/Movies/JianyingPro/User Data/Projects/com.lveditor.draft' : 'D:\\JianyingPro Drafts'} />
              <button className="button button-secondary" type="button" disabled={folderPicking} onClick={chooseDraftFolder}>{folderPicking ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <FolderOpen size={16} aria-hidden="true" />}{folderPicking ? '选择中' : '选择'}</button>
            </div>
          </label>
          {pathCheck.normalized && pathCheck.normalized !== extractPath && <p className="delivery-note"><strong>规范化后：</strong>{pathCheck.normalized}</p>}
          {pathCheck.issues.length > 0 && <div className={`delivery-message${pathCheck.valid ? ' is-warning' : ' is-error'}`}><CircleAlert size={16} aria-hidden="true" /><div>{pathCheck.issues.map(issue => <span key={issue}>{issue}</span>)}</div></div>}
          <fieldset className="delivery-segmented"><legend>剪映所在系统</legend><button type="button" aria-pressed={targetOS === 'mac'} className={targetOS === 'mac' ? 'is-active' : ''} onClick={() => setTargetOS('mac')}>Mac</button><button type="button" aria-pressed={targetOS === 'windows'} className={targetOS === 'windows' ? 'is-active' : ''} onClick={() => setTargetOS('windows')}>Windows</button></fieldset>
          {jobs.draft_local?.result?.draft_path && <p className="delivery-note"><strong>已写入：</strong>{jobs.draft_local.result.draft_path}</p>}
          {jobs.draft_local?.result?.warnings?.length > 0 && <div className="delivery-message is-warning"><CircleAlert size={16} aria-hidden="true" /><div>{jobs.draft_local.result.warnings.map(warning => <span key={warning}>{warning}</span>)}</div></div>}
          <div className="export-job-pair"><JobState job={jobs.draft_local} fallback="尚未写入本地剪映" /><JobState job={jobs.draft} fallback={draftAvailable ? '草稿 ZIP 可下载' : '尚未准备草稿 ZIP'} /></div>
          <div className="export-actions">
            <button className="button button-primary" type="button" disabled={isBusy(jobs.draft_local) || !pathCheck.valid || !canBuildRenderedOutputs} onClick={() => startExport('draft_local')}>{isBusy(jobs.draft_local) ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <HardDriveDownload size={16} aria-hidden="true" />}{isBusy(jobs.draft_local) ? '写入中...' : '写入剪映'}</button>
            {draftAvailable ? <button className="button button-secondary" type="button" onClick={downloadDraft}><Download size={16} aria-hidden="true" />下载草稿 ZIP</button> : <button className="button button-secondary" type="button" disabled={isBusy(jobs.draft) || !canBuildRenderedOutputs} onClick={() => startExport('draft')}>{isBusy(jobs.draft) ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <FileArchive size={16} aria-hidden="true" />}{isBusy(jobs.draft) ? '准备中...' : '准备草稿 ZIP'}</button>}
          </div>
        </section>
      </div>
    </main>
  )
}

function StatusMetric({ label, value, ready = false, warning = false }) {
  return <div><span>{label}</span><strong className={ready ? 'is-ready' : warning ? 'is-warning' : ''}>{ready ? <CheckCircle2 size={15} aria-hidden="true" /> : warning ? <CircleAlert size={15} aria-hidden="true" /> : null}{value}</strong></div>
}

function JobState({ job, fallback }) {
  return (
    <div className={`export-job${job?.status === 'failed' ? ' is-error' : job?.status === 'completed' ? ' is-success' : ''}`} role="status" aria-live="polite">
      <span>{job ? jobLabel(job) : fallback}</span>
      {job?.error && <strong>{job.error}</strong>}
    </div>
  )
}
