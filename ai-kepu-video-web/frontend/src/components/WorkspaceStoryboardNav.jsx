import { Clock3, ImageOff, RefreshCw, Save } from 'lucide-react'
import { normalizeMediaUrl } from '../utils/mediaUrl'
import { normalizeSubtitleText, secondsToLabel, segmentDuration } from '../pages/previewUtils'

function StoryboardSkeleton({ index }) {
  return (
    <article className="workspace-segment-card is-skeleton" style={{ '--segment-order': index }}>
      <header>
        <span className="workspace-segment-number">{String(index + 1).padStart(2, '0')}</span>
        <div><i /><i /></div>
      </header>
      <p className="workspace-segment-summary" />
    </article>
  )
}

export function WorkspaceStoryboardNav({
  scrollRef,
  onScroll,
  workspace,
  stage,
  activeStyle,
  saveMessage,
  onRetrySave,
  onRestore,
  editable,
  busyAction,
  segments,
  selectedIndex,
  onSelect,
  onResegment,
  getSegmentState,
}) {
  const currentSegment = segments[selectedIndex]
  const currentImageUrl = normalizeMediaUrl(currentSegment?.image_url)

  return (
    <section ref={scrollRef} className="workspace-content" aria-label="分镜导航" onScroll={onScroll}>
      <header className="workspace-content-header">
        <div className="workspace-rail-cover">
          {currentImageUrl
            ? <img src={currentImageUrl} alt="当前分镜缩略图" />
            : <div style={{ backgroundImage: `url(${activeStyle?.image || ''})` }}><span /><ImageOff size={17} /></div>}
          <strong>{segments.length ? `片段 ${selectedIndex + 1}` : '等待分镜'}</strong>
        </div>
        <div className="workspace-title-row">
          <div><span>预案</span><h1>{workspace.name}</h1></div>
          <div className="workspace-summary-chips"><span>{workspace.visual_style}</span><span>{workspace.ratio}</span><span>{workspace.text_style}</span></div>
        </div>
        <div className="workspace-sync-row">
          <span className={`workspace-status-pill is-${stage.tone}`}>{stage.title}</span>
          <span className={`workspace-save-state${saveMessage.includes('正在') || saveMessage.includes('等待') ? ' is-saving' : saveMessage.includes('失败') ? ' is-error' : ' is-synced'}`}><Save size={14} />{saveMessage}</span>
          {saveMessage !== '已同步' && <button className="workspace-save-retry" type="button" onClick={onRetrySave}>重试保存</button>}
          {workspace.can_restore_plan && <button className="workspace-plan-restore" type="button" onClick={onRestore} disabled={!editable || Boolean(busyAction)}>恢复拆分前版本</button>}
          <button type="button" onClick={onResegment} disabled={!editable || busyAction === 'resegment'}><RefreshCw size={14} />重新拆分</button>
        </div>
      </header>

      <div className="workspace-section-heading">
        <div><strong>全分镜流</strong><span>选择分镜后，中间预览和右侧编辑器会同步切换。</span></div>
        <span>{workspace.progress.prompts_ready}/{workspace.segments_count} 提示词完成</span>
      </div>

      <div className="workspace-segment-stream">
        {!segments.length
          ? Array.from({ length: 4 }, (_, index) => <StoryboardSkeleton key={index} index={index} />)
          : segments.map((segment, index) => {
              const state = getSegmentState(segment)
              return (
                <article
                  key={segment.id || segment.segment_index}
                  data-workspace-segment={segment.segment_index}
                  role="button"
                  tabIndex={0}
                  aria-current={index === selectedIndex ? 'true' : undefined}
                  aria-label={`选择分镜 ${index + 1}，${state.label}`}
                  className={`workspace-segment-card${index === selectedIndex ? ' is-selected' : ''}`}
                  onClick={() => onSelect(index)}
                  onKeyDown={event => {
                    if (event.key !== 'Enter' && event.key !== ' ') return
                    event.preventDefault()
                    onSelect(index)
                  }}
                  style={{ '--segment-order': index }}
                >
                  <header>
                    <span className="workspace-segment-number">{String(index + 1).padStart(2, '0')}</span>
                    <div>
                      <strong>分镜 {index + 1}</strong>
                      <span><Clock3 size={13} />{segment.duration ? '真实' : '预计'} {secondsToLabel(segmentDuration(segment))}</span>
                    </div>
                    <span className={`workspace-segment-state is-${state.tone}`}>{state.label}</span>
                  </header>
                  <p className="workspace-segment-summary">{normalizeSubtitleText(segment.text || '等待文案')}</p>
                </article>
              )
            })}
      </div>
    </section>
  )
}
