import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Eye,
  History,
  LoaderCircle,
  PanelRightClose,
  PanelRightOpen,
  Save,
  Settings2,
  Upload,
  Volume2,
  WandSparkles,
} from 'lucide-react'
import { secondsToLabel, segmentDuration } from '../pages/previewUtils'
import { VoicePicker } from './VoicePicker'
import { SegmentFailureList } from './SegmentFailureList'
import { AssetHistory } from './ui/AssetHistory'
import { Tooltip } from './ui/Tooltip'
import { VisualStyleCard } from './ui/VisualStyleCard'

function assetStatusLabel(status) {
  return ({ completed: '完成', stale: '待更新', failed: '失败', processing: '生成中', pending: '等待' })[status] || '等待'
}

export function WorkspaceInspector({
  segmentScrollRef,
  settingsScrollRef,
  onSegmentScroll,
  onSettingsScroll,
  open,
  onToggle,
  onClose,
  currentSegment,
  currentState,
  selectedIndex,
  segmentCount,
  onSelectSegment,
  editable,
  onSegmentChange,
  voices,
  workspace,
  operationTarget,
  busyAction,
  onRegenerate,
  onRegeneratePrompt,
  imageUrl,
  imageHistoryOpen,
  imageHistoryVersions,
  selectedHistoryId,
  historyLoading,
  onOpenImage,
  onUploadImage,
  onToggleImageHistory,
  onSelectHistoryVersion,
  storageWarning,
  selectedVoice,
  ttsOptions,
  onVoiceChange,
  onTtsOptionsChange,
  onVoicePreview,
  voicePreviewState,
  onConfirmVoice,
  voiceReady,
  visualStyles,
  onSaveSettings,
  ratioOptions,
  runtimeConfig,
  setRuntimeConfig,
  normalizeRuntime,
  onSaveRuntime,
  onOpenApi,
}) {
  return <aside className={`workspace-settings${open ? ' is-open' : ''}`} aria-label="生产设置">
    <nav className="workspace-inspector-tabs" aria-label="右侧编辑区" role="tablist"><button id="workspace-segment-tab" type="button" role="tab" aria-selected={!open} aria-controls="workspace-segment-panel" className={!open ? 'is-active' : ''} onClick={() => open && onToggle()}>当前分镜</button><button id="workspace-settings-tab" type="button" role="tab" aria-selected={open} aria-controls="workspace-settings-panel" className={open ? 'is-active' : ''} onClick={() => !open && onToggle()}>全片设置{!voiceReady ? <i /> : null}</button></nav>
    <button type="button" className="workspace-settings-toggle" onClick={onToggle} aria-label={open ? '收起设置' : '展开设置'} title={open ? '返回当前分镜设置' : '打开全片设置'}>{open ? <PanelRightClose size={19} /> : <PanelRightOpen size={19} />}</button>
    {!open ? <div ref={segmentScrollRef} id="workspace-segment-panel" className="workspace-segment-inspector" role="tabpanel" aria-labelledby="workspace-segment-tab" onScroll={onSegmentScroll}>
      <header className="workspace-inspector-heading"><div><span>分镜设置</span><h2>{currentSegment ? `分镜 ${selectedIndex + 1}` : '等待分镜'}</h2></div><div><button type="button" onClick={() => onSelectSegment(selectedIndex - 1)} disabled={selectedIndex <= 0} aria-label="上一段"><ChevronLeft size={16} /></button><button type="button" onClick={() => onSelectSegment(selectedIndex + 1)} disabled={selectedIndex >= segmentCount - 1} aria-label="下一段"><ChevronRight size={16} /></button></div></header>
      {currentSegment ? <>
        <div className="workspace-inspector-status"><span className={`workspace-segment-state is-${currentState.tone}`}>{currentState.label}</span><span>{currentSegment.duration ? '真实' : '预计'} {secondsToLabel(segmentDuration(currentSegment))}</span></div>
        <label className="workspace-inspector-field"><span>配音文案</span><textarea value={currentSegment.text} readOnly={!editable} onChange={event => onSegmentChange(currentSegment.segment_index, { text: event.target.value })} /></label>
        <label className="workspace-inspector-field"><span>生图提示词 {currentSegment.prompt_manual ? <em>手工编辑</em> : null}</span>{currentSegment.prompt_status === 'completed' ? <textarea value={currentSegment.image_prompt} readOnly={!editable} onChange={event => onSegmentChange(currentSegment.segment_index, { image_prompt: event.target.value })} /> : <div className="workspace-prompt-skeleton"><i /><i /><i /></div>}</label>
        {currentSegment.prompt_needs_review ? <p className="workspace-inline-warning"><AlertTriangle size={14} />文案已变化，请检查提示词。</p> : null}
        <SegmentFailureList segment={currentSegment} busy={busyAction === `prompt:${currentSegment.segment_index}`} onRetryPrompt={() => onRegeneratePrompt(currentSegment)} />
        <section className="workspace-inspector-section"><div className="workspace-setting-heading"><strong>分镜音色</strong><span>留空时跟随全片音色</span></div><select value={currentSegment.audio_voice_type || ''} disabled={!editable} onChange={event => onSegmentChange(currentSegment.segment_index, { audio_voice_type: event.target.value })}><option value="">跟随全片 · {voices.find(voice => voice.id === workspace.voice_type)?.name || '尚未确认'}</option>{voices.filter(voice => voice.selectable && voice.id !== workspace.voice_type).map(voice => <option key={voice.id} value={voice.id}>{voice.name}</option>)}</select></section>
        <div className="workspace-asset-states"><span className={`is-${currentSegment.image_status}`}>图片 · {assetStatusLabel(currentSegment.image_status)}</span><span className={`is-${currentSegment.audio_status}`}>配音 · {assetStatusLabel(currentSegment.audio_status)}</span></div>
        <div className="workspace-inspector-media-actions" aria-label="当前分镜素材操作">
          <button type="button" disabled={!imageUrl} onClick={onOpenImage}><Eye size={14} />查看画面</button>
          <button type="button" disabled={!editable || Boolean(busyAction)} onClick={onUploadImage}><Upload size={14} />上传替换</button>
          <button type="button" aria-expanded={imageHistoryOpen} disabled={Boolean(busyAction)} onClick={onToggleImageHistory}><History size={14} />历史版本</button>
        </div>
        <div className="workspace-inspector-actions"><Tooltip label={currentSegment.image_prompt ? '只处理当前分镜图片，不会重新生成其他素材' : '请先重新生成这一段的提示词'}><button type="button" disabled={!editable || !currentSegment.image_prompt || currentSegment.prompt_status !== 'completed' || busyAction === `image:${currentSegment.segment_index}`} onClick={() => onRegenerate(currentSegment, 'image')}>{operationTarget(currentSegment, 'image') || busyAction === `image:${currentSegment.segment_index}` ? <LoaderCircle className="spin" size={14} /> : <WandSparkles size={14} />}{currentSegment.image_status === 'completed' ? '重新生成图片' : currentSegment.image_status === 'stale' ? '更新此图' : '重试图片'}</button></Tooltip><Tooltip label="只处理当前分镜配音，不会重新生成其他素材"><button type="button" disabled={!editable || busyAction === `audio:${currentSegment.segment_index}`} onClick={() => onRegenerate(currentSegment, 'audio')}>{operationTarget(currentSegment, 'audio') || busyAction === `audio:${currentSegment.segment_index}` ? <LoaderCircle className="spin" size={14} /> : <Volume2 size={14} />}{currentSegment.audio_status === 'completed' ? '重新生成配音' : currentSegment.audio_status === 'stale' ? '更新配音' : '重试配音'}</button></Tooltip></div>
        {storageWarning ? <p className="workspace-inspector-storage-warning" role="status"><AlertTriangle size={14} /><span>素材仍可使用，但本地归档尚未完成。</span></p> : null}
        {imageHistoryOpen ? <div className="workspace-inspector-history">
          <AssetHistory
            versions={imageHistoryVersions}
            selectedId={selectedHistoryId}
            emptyMessage={historyLoading ? '正在读取历史版本…' : '这个分镜还没有可回选的图片版本'}
            onSelect={busyAction ? undefined : onSelectHistoryVersion}
            onRestore={busyAction ? undefined : onSelectHistoryVersion}
          />
        </div> : null}
      </> : <div className="workspace-inspector-skeleton"><i /><i /><i /><i /><i /></div>}
    </div> : <div ref={settingsScrollRef} id="workspace-settings-panel" className="workspace-settings-panel" role="tabpanel" aria-labelledby="workspace-settings-tab" onScroll={onSettingsScroll}>
      <header><div><span>全片设置</span><h2>画面与配音</h2></div><button type="button" aria-label="收起设置" onClick={onClose}><PanelRightClose size={18} /></button></header>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>配音音色</strong><span>试听与最终配音使用同一组语速参数</span></div><VoicePicker voices={voices} value={selectedVoice} ttsOptions={ttsOptions} onChange={onVoiceChange} onOptionsChange={onTtsOptionsChange} onPreview={onVoicePreview} playingVoice={voicePreviewState.playingVoice} previewLoading={voicePreviewState.loading} previewError={voicePreviewState.error} disabled={!editable} compact /></section>
      <button type="button" className="button button-primary workspace-confirm-voice" disabled={!selectedVoice || busyAction === 'settings' || !editable} onClick={onConfirmVoice}>{busyAction === 'settings' ? '正在保存…' : !editable ? '预案生成完成后确认音色' : voiceReady ? '更新全片音色' : '确认音色并返回预案'}</button>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>画面风格</strong><span>保存后由你确认是否重新生成系统提示词</span></div><div className="workspace-style-grid">{visualStyles.map(style => <VisualStyleCard key={style.value} style={style} disabled={!editable} selected={workspace.visual_style === style.value} onSelect={value => onSaveSettings({ visual_style: value, voice_confirmed: voiceReady })} />)}</div></section>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>视频比例</strong><span>已有图片会标记为待更新</span></div><div className="workspace-ratio-control">{ratioOptions.map(ratio => <button type="button" key={ratio} disabled={!editable} className={workspace.ratio === ratio ? 'is-selected' : ''} onClick={() => onSaveSettings({ ratio, voice_confirmed: voiceReady })}>{ratio}</button>)}</div></section>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>生成策略</strong><span>保存后作为下一批生成与失败重试的默认值</span></div><div className="workspace-runtime-grid"><label><span>提示词并发</span><input type="number" min="1" max="8" value={runtimeConfig.prompt_concurrency} onChange={event => setRuntimeConfig(current => ({ ...current, prompt_concurrency: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>配音并发</span><input type="number" min="1" max="8" value={runtimeConfig.tts_concurrency} onChange={event => setRuntimeConfig(current => ({ ...current, tts_concurrency: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>生图并发</span><input type="number" min="1" max="8" value={runtimeConfig.image_concurrency} onChange={event => setRuntimeConfig(current => ({ ...current, image_concurrency: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>失败后重试</span><input type="number" min="0" max="5" value={runtimeConfig.retry_count} onChange={event => setRuntimeConfig(current => ({ ...current, retry_count: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>重试间隔（秒）</span><input type="number" min="1" max="60" value={runtimeConfig.retry_interval_seconds} onChange={event => setRuntimeConfig(current => ({ ...current, retry_interval_seconds: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label></div><p className="workspace-runtime-note">普通失败按基础间隔逐次递增；限流优先遵循服务商等待时间。Agnes 默认 8 路并发，滚动 60 秒最多派发 20 个请求。</p><button type="button" className="button button-secondary workspace-runtime-save" disabled={busyAction === 'runtime'} onClick={onSaveRuntime}>{busyAction === 'runtime' ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}{busyAction === 'runtime' ? '正在保存…' : '保存生成策略'}</button></section>
      <button type="button" className="button button-secondary workspace-api-button" onClick={onOpenApi}><Settings2 size={16} />打开 API 配置</button>
    </div>}
  </aside>
}
