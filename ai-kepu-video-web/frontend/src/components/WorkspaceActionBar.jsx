import { Download, LoaderCircle, Play, RefreshCw, Sparkles, Square, Volume2 } from 'lucide-react'
import { Tooltip } from './ui/Tooltip'

export function WorkspaceActionBar({
  workspace,
  stage,
  voiceReady,
  voiceLabel,
  visualPlanReady,
  savingCount,
  busyAction,
  recoverable,
  canResume,
  recoverySummary,
  recoveryActionLabel,
  canRenderFullVideo,
  renderingFullVideo,
  fullVideoJobActive,
  cancellingFullVideo,
  previewPollingFailed,
  previewValid,
  canEnterExport,
  onResume,
  onConfirmVoice,
  onGenerateAssets,
  onCancelProduction,
  onFullVideo,
  onCancelFullVideo,
  onExport,
}) {
  const flow = workspace.production
  const active = ['queued', 'generating_assets', 'render_queued', 'rendering', 'cancelling'].includes(flow?.state)
  const labels = { queued: '等待生成名额', generating_assets: '正在生成图片与配音', render_queued: '排队渲染', rendering: '正在生成 MP4', cancelling: '正在停止生产', completed: '成片已完成', failed: '生产失败，可保留素材重试', stale: '预案已更新，请重新确认', cancelled: '生产已停止' }
  const canProduce = !active && visualPlanReady && (workspace.stage === 'awaiting_confirmation' || workspace.stage === 'awaiting_finalization' || (flow && flow.state !== 'completed'))
  return <footer className="workspace-actionbar">
    <div className="workspace-action-status">
      <strong>{labels[flow?.state] || stage.title}</strong>
      <span>{flow?.error || (canProduce ? `采用当前音色：${voiceLabel}。确认后自动生成图片、配音和视频，试听与预览均可选。` : recoverable ? recoverySummary : stage.description)}</span>
    </div>
    <div>
      {canProduce ? <button type="button" className="button button-primary" disabled={Boolean(busyAction)} onClick={onGenerateAssets}>{busyAction ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}{flow ? '重试并生成视频' : '确认并生成视频'}</button> : null}
      {!canProduce && !active && !flow && recoverable && canResume ? <button type="button" className="button button-primary" disabled={Boolean(busyAction)} onClick={onResume}><RefreshCw size={16} />{recoveryActionLabel}</button> : null}
      {active ? <button type="button" className="button button-secondary" disabled={flow.state === 'cancelling'} onClick={onCancelProduction}><Square size={15} />停止成片生产</button> : null}
      {!active && !canProduce && canRenderFullVideo ? <button type="button" className="button button-secondary" disabled={renderingFullVideo} onClick={onFullVideo}>{renderingFullVideo ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}{renderingFullVideo ? '正在生成完整视频…' : previewPollingFailed ? '重新连接视频生成' : previewValid ? '重新生成视频' : '生成视频'}</button> : null}
      {!active && fullVideoJobActive ? <button type="button" className="button button-secondary" disabled={cancellingFullVideo} onClick={onCancelFullVideo}><Square size={15} />{cancellingFullVideo ? '正在取消…' : '取消生成'}</button> : null}
      <button type="button" className="button button-secondary" disabled={!canEnterExport} onClick={onExport}><Download size={16} />导出剪映草稿或素材</button>
    </div>
  </footer>
}
