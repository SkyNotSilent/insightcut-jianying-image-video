import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SegmentFailureList } from './SegmentFailureList'

describe('SegmentFailureList', () => {
  it('routes prompt failures through the exact prompt operation', async () => {
    const user = userEvent.setup()
    const onRetryPrompt = vi.fn()
    render(<SegmentFailureList segment={{ segment_index: 3, prompt_status: 'failed', prompt_error_code: 'rate_limit', image_status: 'pending', audio_status: 'completed' }} onRetryPrompt={onRetryPrompt} />)

    expect(screen.getByText('提示词 · 服务请求过于频繁')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新生成提示词' }))
    expect(onRetryPrompt).toHaveBeenCalledTimes(1)
  })

  it('does not offer prompt regeneration for an image-only failure', () => {
    render(<SegmentFailureList segment={{ segment_index: 1, prompt_status: 'completed', image_status: 'failed', image_error_code: 'provider_error', audio_status: 'completed' }} onRetryPrompt={() => {}} />)
    expect(screen.queryByRole('button', { name: '重新生成提示词' })).not.toBeInTheDocument()
  })

  it('shows an actionable prompt-edit message for content policy rejection', () => {
    render(<SegmentFailureList segment={{
      segment_index: 1,
      prompt_status: 'completed',
      image_status: 'failed',
      image_error_code: 'content_policy',
      image_error_meta: { retryable: false },
      audio_status: 'completed',
    }} onRetryPrompt={() => {}} />)

    expect(screen.getByText('图片 · 画面描述未通过内容检查')).toBeInTheDocument()
    expect(screen.getByText(/修改当前分镜的生图提示词/)).toBeInTheDocument()
  })
})
