import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Eye,
  History,
  Image as ImageIcon,
  Images,
  LoaderCircle,
  PanelRightClose,
  PanelRightOpen,
  Save,
  Settings2,
  Subtitles,
  Upload,
  Volume2,
  WandSparkles,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { createProductionTemplate, getAssetLibrary, listProductionTemplates, selectSegmentAsset } from '../api/task'
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
  taskId,
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
  onOpenImage,
  onUploadImage,
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
  onAssetSelected,
}) {
  const [activeTab, setActiveTab] = useState(open ? 'settings' : 'segment')
  const [assetKind, setAssetKind] = useState('image')
  const [library, setLibrary] = useState([])
  const [libraryLoading, setLibraryLoading] = useState(false)
  const [libraryError, setLibraryError] = useState('')
  const [templates, setTemplates] = useState([])
  const [templateChoice, setTemplateChoice] = useState('')

  useEffect(() => {
    if (open && activeTab === 'segment') setActiveTab('settings')
    if (!open && activeTab === 'settings') setActiveTab('segment')
  }, [open])

  const changeTab = tab => {
    setActiveTab(tab)
    if (tab === 'segment' && open) onToggle()
    if (tab !== 'segment' && !open) onToggle()
  }

  useEffect(() => {
    if (!taskId || !['versions', 'project'].includes(activeTab)) return
    let alive = true
    setLibraryLoading(true)
    setLibraryError('')
    const params = activeTab === 'versions'
      ? { asset_type: assetKind, scope: 'segment', segment_index: currentSegment?.segment_index, page_size: 60 }
      : { asset_type: assetKind, scope: 'project', page_size: 100 }
    getAssetLibrary(taskId, params)
      .then(result => { if (alive) setLibrary(result?.items || []) })
      .catch(() => { if (alive) setLibraryError('素材版本暂时无法读取') })
      .finally(() => { if (alive) setLibraryLoading(false) })
    return () => { alive = false }
  }, [activeTab, assetKind, currentSegment?.segment_index, taskId, workspace.snapshot_key])

  useEffect(() => {
    if (activeTab !== 'settings') return
    listProductionTemplates().then(result => setTemplates(result?.items || [])).catch(() => setTemplates([]))
  }, [activeTab])

  const libraryVersions = useMemo(() => library.map(asset => ({
    ...asset,
    id: asset.asset_id,
    label: asset.label,
    thumbnail: assetKind === 'image' ? (asset.url || asset.file_url) : '',
    createdAt: asset.created_at,
    status: asset.is_selected ? 'complete' : 'ready',
    restorable: !asset.is_selected,
  })), [assetKind, library])
  const selectedAssetId = assetKind === 'image'
    ? currentSegment?.selected_image_asset_id
    : currentSegment?.selected_audio_asset_id

  const chooseAsset = async version => {
    if (!currentSegment || busyAction) return
    const request = {
      snapshot_key: workspace.snapshot_key,
      asset_id: version.asset_id || version.id,
      asset_type: assetKind,
      confirm_text_mismatch: false,
    }
    try {
      await selectSegmentAsset(taskId, currentSegment.segment_index, request)
    } catch (error) {
      const detail = error?.response?.data?.detail
      if (detail?.code !== 'audio_text_mismatch' || !window.confirm(`${detail.message}\n\n确认后仍可使用，但工作台会持续标记文案可能不一致。`)) return
      await selectSegmentAsset(taskId, currentSegment.segment_index, { ...request, confirm_text_mismatch: true })
    }
    onAssetSelected?.()
    const result = await getAssetLibrary(taskId, activeTab === 'versions'
      ? { asset_type: assetKind, scope: 'segment', segment_index: currentSegment.segment_index, page_size: 60 }
      : { asset_type: assetKind, scope: 'project', page_size: 100 })
    setLibrary(result?.items || [])
  }

  const saveAsTemplate = async () => {
    const name = window.prompt('为这套生产配置命名', `${workspace.name || '当前项目'} 预设`)
    if (!name?.trim()) return
    await createProductionTemplate({
      name: name.trim(),
      visual_style: workspace.visual_style,
      text_style: workspace.text_style,
      ratio: workspace.ratio,
      voice_type: selectedVoice || workspace.voice_type,
      tts_options: ttsOptions,
      subtitle_options: workspace.subtitle_options,
      generation_options: workspace.generation_options || runtimeConfig,
    })
    window.dispatchEvent(new CustomEvent('insightcut:template-saved'))
  }

  const applyTemplate = async () => {
    const template = templates.find(item => item.template_id === templateChoice)
    if (!template) return
    const effects = []
    if (template.visual_style !== workspace.visual_style || template.ratio !== workspace.ratio) effects.push('已有图片会标记为待更新')
    if (template.voice_type && template.voice_type !== workspace.voice_type) effects.push('已有配音会标记为待更新，并需要重新确认音色')
    if (JSON.stringify(template.subtitle_options || {}) !== JSON.stringify(workspace.subtitle_options || {})) effects.push('完整视频与草稿会过期，图片和配音不变')
    effects.push('生成策略只影响后续操作，不会自动消耗额度')
    if (!window.confirm(`应用“${template.name}”？\n\n${effects.map(item => `• ${item}`).join('\n')}`)) return
    if (template.voice_type) onVoiceChange(template.voice_type)
    if (template.tts_options) onTtsOptionsChange(template.tts_options)
    await onSaveSettings({
      template_id: template.template_id,
      text_style: template.text_style,
      visual_style: template.visual_style,
      ratio: template.ratio,
      voice_type: template.voice_type || workspace.voice_type,
      tts_options: template.tts_options || workspace.tts_options,
      voice_confirmed: template.voice_type === workspace.voice_type && voiceReady,
      subtitle_options: template.subtitle_options,
      generation_options: template.generation_options,
    })
  }

  const tabs = [
    { id: 'segment', label: '当前分镜' },
    { id: 'versions', label: '素材版本' },
    { id: 'project', label: '项目素材' },
    { id: 'settings', label: '全片设置', attention: !voiceReady },
  ]

  return <aside className={`workspace-settings${activeTab !== 'segment' ? ' is-open' : ''}`} aria-label="生产设置">
    <nav className="workspace-inspector-tabs" aria-label="右侧编辑区" role="tablist">{tabs.map(tab => <button key={tab.id} id={`workspace-${tab.id}-tab`} type="button" role="tab" aria-selected={activeTab === tab.id} className={activeTab === tab.id ? 'is-active' : ''} onClick={() => changeTab(tab.id)}>{tab.label}{tab.attention ? <i /> : null}</button>)}</nav>
    <button type="button" className="workspace-settings-toggle" onClick={onToggle} aria-label={open ? '收起设置' : '展开设置'} title={open ? '返回当前分镜设置' : '打开全片设置'}>{open ? <PanelRightClose size={19} /> : <PanelRightOpen size={19} />}</button>
    {activeTab === 'segment' ? <div ref={segmentScrollRef} id="workspace-segment-panel" className="workspace-segment-inspector" role="tabpanel" aria-labelledby="workspace-segment-tab" onScroll={onSegmentScroll}>
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
          <button type="button" disabled={Boolean(busyAction)} onClick={() => changeTab('versions')}><History size={14} />素材版本</button>
        </div>
        <div className="workspace-inspector-actions"><Tooltip label={currentSegment.image_prompt ? '只处理当前分镜图片，不会重新生成其他素材' : '请先重新生成这一段的提示词'}><button type="button" disabled={!editable || !currentSegment.image_prompt || currentSegment.prompt_status !== 'completed' || busyAction === `image:${currentSegment.segment_index}`} onClick={() => onRegenerate(currentSegment, 'image')}>{operationTarget(currentSegment, 'image') || busyAction === `image:${currentSegment.segment_index}` ? <LoaderCircle className="spin" size={14} /> : <WandSparkles size={14} />}{currentSegment.image_status === 'completed' ? '重新生成图片' : currentSegment.image_status === 'stale' ? '更新此图' : '重试图片'}</button></Tooltip><Tooltip label="只处理当前分镜配音，不会重新生成其他素材"><button type="button" disabled={!editable || busyAction === `audio:${currentSegment.segment_index}`} onClick={() => onRegenerate(currentSegment, 'audio')}>{operationTarget(currentSegment, 'audio') || busyAction === `audio:${currentSegment.segment_index}` ? <LoaderCircle className="spin" size={14} /> : <Volume2 size={14} />}{currentSegment.audio_status === 'completed' ? '重新生成配音' : currentSegment.audio_status === 'stale' ? '更新配音' : '重试配音'}</button></Tooltip></div>
        {storageWarning ? <p className="workspace-inspector-storage-warning" role="status"><AlertTriangle size={14} /><span>素材仍可使用，但本地归档尚未完成。</span></p> : null}
      </> : <div className="workspace-inspector-skeleton"><i /><i /><i /><i /><i /></div>}
    </div> : ['versions', 'project'].includes(activeTab) ? <div className="workspace-asset-library-panel" role="tabpanel" aria-labelledby={`workspace-${activeTab}-tab`}>
      <header className="workspace-inspector-heading"><div><span>{activeTab === 'versions' ? '当前分镜历史' : '当前项目素材池'}</span><h2>{activeTab === 'versions' ? `分镜 ${selectedIndex + 1} 版本` : '复用项目素材'}</h2></div>{activeTab === 'project' ? <Images size={19} /> : <History size={19} />}</header>
      <div className="workspace-asset-kind-tabs"><button type="button" className={assetKind === 'image' ? 'is-active' : ''} onClick={() => setAssetKind('image')}><ImageIcon size={14} />图片</button><button type="button" className={assetKind === 'audio' ? 'is-active' : ''} onClick={() => setAssetKind('audio')}><Volume2 size={14} />配音</button></div>
      <p className="workspace-library-guidance">{activeTab === 'versions' ? '每次生成、重新生成和上传都会保留。恢复版本不会调用模型。' : '可从当前项目的其他分镜复用素材；配音文案不一致时会先要求确认。'}</p>
      {currentSegment?.audio_mismatch_confirmed && assetKind === 'audio' ? <p className="workspace-inline-warning"><AlertTriangle size={14} />当前配音来自不同文案，导出前请再次试听确认。</p> : null}
      {libraryError ? <p className="workspace-inline-error"><AlertTriangle size={14} />{libraryError}</p> : null}
      {assetKind === 'image' ? <AssetHistory
        versions={libraryVersions}
        selectedId={selectedAssetId}
        title={activeTab === 'versions' ? '画面版本' : '项目图片'}
        emptyMessage={libraryLoading ? '正在读取图片版本…' : '当前范围还没有图片素材'}
        onSelect={busyAction ? undefined : chooseAsset}
        onRestore={busyAction ? undefined : chooseAsset}
      /> : <AssetHistory
        versions={libraryVersions}
        selectedId={selectedAssetId}
        title={activeTab === 'versions' ? '配音版本' : '项目配音'}
        emptyMessage={libraryLoading ? '正在读取配音版本…' : '当前范围还没有配音素材'}
        onSelect={busyAction ? undefined : chooseAsset}
        onRestore={busyAction ? undefined : chooseAsset}
        renderPreview={version => version.url || version.file_url ? <audio controls preload="none" src={version.url || version.file_url} onClick={event => event.stopPropagation()} /> : <Volume2 size={20} />}
      />}
      {activeTab === 'project' ? <div className="workspace-project-library-note"><Subtitles size={15} /><span>字幕仍由当前分镜文案统一生成，不会被跨分镜素材覆盖。</span></div> : null}
    </div> : <div ref={settingsScrollRef} id="workspace-settings-panel" className="workspace-settings-panel" role="tabpanel" aria-labelledby="workspace-settings-tab" onScroll={onSettingsScroll}>
      <header><div><span>全片设置</span><h2>画面与配音</h2></div><button type="button" aria-label="收起设置" onClick={onClose}><PanelRightClose size={18} /></button></header>
      <section className="workspace-setting-section workspace-template-apply"><div className="workspace-setting-heading"><strong>生产模板</strong><span>应用前会展示真实影响范围，不会自动重新生成</span></div><div><select value={templateChoice} onChange={event => setTemplateChoice(event.target.value)}><option value="">选择已保存模板</option>{templates.map(template => <option key={template.template_id} value={template.template_id}>{template.name}{template.is_default ? ' · 默认' : ''}</option>)}</select><button type="button" className="button button-secondary" disabled={!templateChoice || busyAction === 'settings'} onClick={applyTemplate}>应用</button></div></section>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>配音音色</strong><span>试听与最终配音使用同一组语速参数</span></div><VoicePicker voices={voices} value={selectedVoice} ttsOptions={ttsOptions} onChange={onVoiceChange} onOptionsChange={onTtsOptionsChange} onPreview={onVoicePreview} playingVoice={voicePreviewState.playingVoice} previewLoading={voicePreviewState.loading} previewError={voicePreviewState.error} disabled={!editable} compact /></section>
      <button type="button" className="button button-primary workspace-confirm-voice" disabled={!selectedVoice || busyAction === 'settings' || !editable} onClick={onConfirmVoice}>{busyAction === 'settings' ? '正在保存…' : !editable ? '预案生成完成后确认音色' : voiceReady ? '更新全片音色' : '确认音色并返回预案'}</button>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>画面风格</strong><span>保存后由你确认是否重新生成系统提示词</span></div><div className="workspace-style-grid">{visualStyles.map(style => <VisualStyleCard key={style.value} style={style} disabled={!editable} selected={workspace.visual_style === style.value} onSelect={value => onSaveSettings({ visual_style: value, voice_confirmed: voiceReady })} />)}</div></section>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>视频比例</strong><span>已有图片会标记为待更新</span></div><div className="workspace-ratio-control">{ratioOptions.map(ratio => <button type="button" key={ratio} disabled={!editable} className={workspace.ratio === ratio ? 'is-selected' : ''} onClick={() => onSaveSettings({ ratio, voice_confirmed: voiceReady })}>{ratio}</button>)}</div></section>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>字幕基础设置</strong><span>即时预览、完整视频和剪映草稿共用这一份任务快照</span></div><div className="workspace-subtitle-options"><label><span>字号</span><select value={workspace.subtitle_options?.size || 'standard'} onChange={event => onSaveSettings({ subtitle_options: { ...(workspace.subtitle_options || {}), size: event.target.value }, voice_confirmed: voiceReady })}><option value="small">小</option><option value="standard">标准</option><option value="large">大</option></select></label><label><span>垂直位置</span><select value={workspace.subtitle_options?.position || 'standard'} onChange={event => onSaveSettings({ subtitle_options: { ...(workspace.subtitle_options || {}), position: event.target.value }, voice_confirmed: voiceReady })}><option value="low">偏低</option><option value="standard">标准</option><option value="high">偏高</option></select></label><label><span>描边</span><select value={workspace.subtitle_options?.outline || 'standard'} onChange={event => onSaveSettings({ subtitle_options: { ...(workspace.subtitle_options || {}), outline: event.target.value }, voice_confirmed: voiceReady })}><option value="light">轻</option><option value="standard">标准</option><option value="strong">强</option></select></label></div></section>
      <section className="workspace-setting-section"><div className="workspace-setting-heading"><strong>生成策略</strong><span>保存后作为下一批生成与失败重试的默认值</span></div><div className="workspace-runtime-grid"><label><span>提示词并发</span><input type="number" min="1" max="8" value={runtimeConfig.prompt_concurrency} onChange={event => setRuntimeConfig(current => ({ ...current, prompt_concurrency: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>配音并发</span><input type="number" min="1" max="8" value={runtimeConfig.tts_concurrency} onChange={event => setRuntimeConfig(current => ({ ...current, tts_concurrency: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>生图并发</span><input type="number" min="1" max="8" value={runtimeConfig.image_concurrency} onChange={event => setRuntimeConfig(current => ({ ...current, image_concurrency: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>失败后重试</span><input type="number" min="0" max="5" value={runtimeConfig.retry_count} onChange={event => setRuntimeConfig(current => ({ ...current, retry_count: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label><label><span>重试间隔（秒）</span><input type="number" min="1" max="60" value={runtimeConfig.retry_interval_seconds} onChange={event => setRuntimeConfig(current => ({ ...current, retry_interval_seconds: event.target.value }))} onBlur={() => setRuntimeConfig(current => normalizeRuntime(current))} /></label></div><p className="workspace-runtime-note">普通失败按基础间隔逐次递增；限流优先遵循服务商等待时间。Agnes 默认 8 路并发，滚动 60 秒最多派发 20 个请求。</p><button type="button" className="button button-secondary workspace-runtime-save" disabled={busyAction === 'runtime'} onClick={onSaveRuntime}>{busyAction === 'runtime' ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}{busyAction === 'runtime' ? '正在保存…' : '保存生成策略'}</button></section>
      <button type="button" className="button button-secondary workspace-api-button" onClick={onOpenApi}><Settings2 size={16} />打开 API 配置</button>
      <button type="button" className="button button-secondary workspace-api-button" onClick={saveAsTemplate}><Save size={16} />保存为生产模板</button>
    </div>}
  </aside>
}
