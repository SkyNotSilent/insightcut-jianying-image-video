import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { AssetHistory } from './AssetHistory'
import { AudioCard } from './AudioCard'
import { ImageCard } from './ImageCard'
import { Lightbox } from './Lightbox'
import { VisualStyleCard } from './VisualStyleCard'

describe('VisualStyleCard', () => {
  it('keeps the visible label and description without a duplicate hover tooltip', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(<VisualStyleCard style={{ value: 'cinematic', label: '电影质感', description: '真实光影与镜头叙事', image: '/cinematic.png' }} selected onSelect={onSelect} />)

    const card = screen.getByRole('button', { name: /电影质感.*真实光影与镜头叙事/ })
    expect(screen.getByText('电影质感')).toBeInTheDocument()
    expect(screen.getByText('真实光影与镜头叙事')).toBeInTheDocument()
    await user.hover(card)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    await user.click(card)
    expect(onSelect).toHaveBeenCalledWith('cinematic')
  })
})

describe('ImageCard', () => {
  it.each([
    ['waiting', '等待生成'],
    ['generating', '正在生成'],
    ['complete', '已完成'],
    ['failed', '生成失败'],
    ['stale', '待更新'],
  ])('renders the %s state without changing its media geometry', (status, label) => {
    const { container } = render(
      <ImageCard status={status} ratio="9 / 16" title="第 03 段" src={status === 'complete' ? '/frame.png' : ''} />,
    )

    expect(screen.getByRole('status', { name: label })).toBeInTheDocument()
    expect(container.querySelector('.asset-card-media')).toHaveStyle({ aspectRatio: '9 / 16' })
  })

  it('uses centralized safe error copy and delegates every operation to callbacks', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    const onOpen = vi.fn()
    const { rerender } = render(
      <ImageCard status="failed" title="第 12 段图片" error={{ error_code: 'rate_limit', error_meta: { retry_after_seconds: 18 } }} onRetry={onRetry} />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent('服务请求过于频繁')
    expect(screen.getByRole('alert')).toHaveTextContent('18 秒')
    await user.click(screen.getByRole('button', { name: '重试图片' }))
    expect(onRetry).toHaveBeenCalledTimes(1)

    rerender(<ImageCard status="complete" title="第 12 段图片" src="/frame.png" onOpen={onOpen} />)
    await user.click(screen.getByRole('button', { name: '查看第 12 段图片' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })
})

describe('AudioCard', () => {
  it('keeps stale audio playable and exposes the precise update action', async () => {
    const user = userEvent.setup()
    const onPlay = vi.fn()
    const onUpdate = vi.fn()
    render(
      <AudioCard
        status="stale"
        title="第 04 段配音"
        src="/voice.wav"
        voiceLabel="讲解小明"
        duration="4.2 秒"
        transcript="旧版本仍然可以试听。"
        onPlay={onPlay}
        onUpdate={onUpdate}
      />,
    )

    expect(screen.getByRole('status', { name: '待更新' })).toBeInTheDocument()
    expect(screen.getByText('讲解小明 · 4.2 秒')).toBeInTheDocument()
    fireEvent.play(screen.getByText('当前浏览器不支持音频播放。'))
    expect(onPlay).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '更新配音' }))
    expect(onUpdate).toHaveBeenCalledTimes(1)
  })
})

describe('AssetHistory', () => {
  it('renders a horizontal ordered version rail and delegates selection/restoration', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const onRestore = vi.fn()
    const versions = [
      { id: 'v2', label: '当前版本', createdAt: '刚刚', status: 'complete', source: 'upload' },
      { id: 'v1', label: '初始版本', createdAt: '10 分钟前', status: 'stale', source: 'regenerated' },
    ]
    render(<AssetHistory versions={versions} selectedId="v2" onSelect={onSelect} onRestore={onRestore} />)

    expect(screen.getByRole('button', { name: '当前版本 刚刚' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('上传替换')).toBeInTheDocument()
    expect(screen.getByText('重新生成')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '初始版本 10 分钟前' }))
    expect(onSelect).toHaveBeenCalledWith(versions[1], 1)
    await user.click(screen.getByRole('button', { name: '恢复初始版本' }))
    expect(onRestore).toHaveBeenCalledWith(versions[1], 1)
  })
})

const LIGHTBOX_ITEMS = [
  { id: 'one', src: '/one.png', title: '第一张', alt: '第一张画面' },
  { id: 'two', src: '/two.png', title: '第二张', alt: '第二张画面' },
]

function LightboxHarness({ onClose, onIndexChange }) {
  const [open, setOpen] = useState(false)
  const close = () => {
    setOpen(false)
    onClose()
  }

  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>查看成片画面</button>
      <Lightbox
        open={open}
        items={LIGHTBOX_ITEMS}
        onClose={close}
        onIndexChange={onIndexChange}
        promptSlot={(item) => <p>{item?.title}的提示词</p>}
        actionSlot={<button type="button">替换当前图片</button>}
      />
    </>
  )
}

describe('Lightbox', () => {
  it('supports arrow navigation, focus trapping, Escape, slots, and opener restoration', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    const onIndexChange = vi.fn()
    render(<LightboxHarness onClose={onClose} onIndexChange={onIndexChange} />)

    const opener = screen.getByRole('button', { name: '查看成片画面' })
    await user.click(opener)
    const close = screen.getByRole('button', { name: '关闭素材查看器' })
    await waitFor(() => expect(close).toHaveFocus())
    expect(screen.getByText('第一张的提示词')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '替换当前图片' })).toBeInTheDocument()

    await user.keyboard('{ArrowRight}')
    expect(onIndexChange).toHaveBeenLastCalledWith(1, LIGHTBOX_ITEMS[1])
    expect(screen.getByRole('img', { name: '第二张画面' })).toBeInTheDocument()
    expect(screen.getByText('第二张的提示词')).toBeInTheDocument()

    await user.keyboard('{ArrowLeft}')
    expect(onIndexChange).toHaveBeenLastCalledWith(0, LIGHTBOX_ITEMS[0])

    close.focus()
    await user.tab({ shift: true })
    expect(screen.getByRole('button', { name: '替换当前图片' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(opener).toHaveFocus()
  })
})
