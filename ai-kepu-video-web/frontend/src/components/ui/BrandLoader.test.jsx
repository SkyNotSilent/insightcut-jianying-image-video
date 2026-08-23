import { render, screen } from '@testing-library/react'
import { BrandLoader } from './BrandLoader'

describe('BrandLoader', () => {
  it('uses the InsightCut reticle as the only visible loading treatment', () => {
    const { container } = render(<BrandLoader label="正在恢复生产工作台" />)

    expect(screen.getByRole('status', { name: '正在恢复生产工作台' })).toBeInTheDocument()
    expect(screen.queryByText('正在恢复生产工作台')).not.toBeInTheDocument()
    expect(container.querySelectorAll('.ui-brand-loader-corner')).toHaveLength(4)
    expect(container.querySelector('.ui-brand-loader-glow')).toBeInTheDocument()
    expect(container.querySelector('.ui-brand-loader-dot')).toBeInTheDocument()
  })
})
