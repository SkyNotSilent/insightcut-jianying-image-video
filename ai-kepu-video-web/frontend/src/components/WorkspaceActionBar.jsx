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
  onFullVideo,
  onCancelFullVideo,
  onExport,
}) {
  const waitingForConfirmation = workspace.stage === 'awaiting_confirmation'
  return <footer className="workspace-actionbar">
    <div className="workspace-action-status">
      <strong>{waitingForConfirmation ? '生成前确认' : stage.title}</strong>
      {waitingForConfirmation
        ? <div className="workspace-confirmation-steps" aria-label="生成前确认步骤">
            <span className={voiceReady ? 'is-ready' : 'is-current'}>1 音色 · {voiceReady ? voiceLabel : '待确认'}</span>
            <span className={visualPlanReady ? 'is-ready' : 'is-pending'}>2 画面方案 · {visualPlanReady ? '待你确认' : '生成中'}</span>
            <span className="is-pending">3 生成图片与配音</span>
          </div>
        : <span>{recoverable ? recoverySummary : voiceReady ? `全片音色：${voiceLabel}` : stage.description}</span>}
    </div>
    <div>
      {recoverable && canResume ? <button type="button" className="button button-primary" disabled={Boolean(busyAction)} onClick={onResume}>{busyAction === 'resume' ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}{busyAction === 'resume' ? '正在恢复…' : recoveryActionLabel}</button> : null}
      {waitingForConfirmation && !voiceReady ? <Tooltip label="验证并确认右侧当前选中的全片音色" placement="top"><button type="button" className="button button-primary" disabled={Boolean(busyAction)} onClick={onConfirmVoice}>{busyAction ? <LoaderCircle className="spin" size={16} /> : <Volume2 size={16} />}{busyAction ? '正在验证音色…' : '确认全片音色'}</button></Tooltip> : null}
      {waitingForConfirmation && voiceReady ? <Tooltip label="确认当前提示词和画面设置，再开始消耗生成额度" placement="top"><button type="button" className="button button-primary" disabled={!visualPlanReady || savingCount > 0 || busyAction === 'generate'} onClick={onGenerateAssets}>{busyAction === 'generate' ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}确认画面方案并生成素材</button></Tooltip> : null}
      {canRenderFullVideo ? <Tooltip label="按需合成完整视频；未点击时不会生成 MP4" placement="top"><button type="button" className="button button-secondary" disabled={renderingFullVideo} onClick={onFullVideo}>{renderingFullVideo ? <LoaderCircle className="spin" size={16} /> : previewPollingFailed ? <RefreshCw size={16} /> : <Play size={16} />}{renderingFullVideo ? '正在生成完整视频…' : previewPollingFailed ? '重新连接视频生成' : previewValid ? '重新生成完整视频预览' : '生成完整视频预览'}</button></Tooltip> : null}
      {fullVideoJobActive ? <Tooltip label="停止派发后续渲染片段；已完成内容和上一份视频会保留" placement="top"><button type="button" className="button button-secondary" disabled={cancellingFullVideo} onClick={onCancelFullVideo}><Square size={15} />{cancellingFullVideo ? '正在取消…' : '取消生成'}</button></Tooltip> : null}
      <button type="button" className={`button ${recoverable && canResume ? 'button-secondary' : 'button-primary'}`} disabled={!canEnterExport} onClick={onExport}><Download size={16} />进入导出中心</button>
    </div>
  </footer>
}
