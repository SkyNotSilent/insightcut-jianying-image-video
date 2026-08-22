import { Download, FileText, FolderKanban, LayoutDashboard, LayoutTemplate, Settings } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { NavLink, useLocation } from 'react-router'

const BRAND_MESSAGES = ['AI 视频工作台', '文稿变成视频', '自由二次编辑', '导入剪映草稿']

function readLastWorkspace() {
  try {
    const value = JSON.parse(localStorage.getItem('insightcut:last-workspace') || 'null')
    return value?.taskId ? value : null
  } catch {
    return null
  }
}

export function BrandNavigation() {
  const location = useLocation()
  const [lastWorkspace, setLastWorkspace] = useState(readLastWorkspace)
  const [messageIndex, setMessageIndex] = useState(0)
  const workspaceMatch = location.pathname.match(/^\/workspace\/([^/]+)/)
  const exportMatch = location.pathname.match(/^\/export\/([^/]+)/)
  const activeTaskId = workspaceMatch?.[1] || exportMatch?.[1] || lastWorkspace?.taskId

  const navigationItems = useMemo(() => [
    { to: '/manuscript', label: '文稿', icon: FileText },
    { to: activeTaskId ? `/workspace/${activeTaskId}` : '/assets?open=workspace', label: '工作台', icon: LayoutDashboard },
    { to: activeTaskId ? `/export/${activeTaskId}` : '/assets?open=export', label: '导出中心', icon: Download },
    { to: '/assets', label: '项目资产', icon: FolderKanban },
    { to: '/templates', label: '模板库', icon: LayoutTemplate },
    { to: workspaceMatch ? `/workspace/${workspaceMatch[1]}/settings` : '/settings', label: '设置', icon: Settings },
  ], [activeTaskId, workspaceMatch])

  useEffect(() => {
    const refresh = () => setLastWorkspace(readLastWorkspace())
    window.addEventListener('storage', refresh)
    window.addEventListener('insightcut:workspace', refresh)
    refresh()
    return () => {
      window.removeEventListener('storage', refresh)
      window.removeEventListener('insightcut:workspace', refresh)
    }
  }, [location.pathname])

  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return undefined
    const timer = window.setInterval(() => setMessageIndex(value => (value + 1) % BRAND_MESSAGES.length), 3200)
    return () => window.clearInterval(timer)
  }, [])

  return <aside className="app-rail" aria-label="InsightCut 主导航">
    <NavLink className="rail-brand" to="/manuscript" aria-label="InsightCut 文稿首页">
      <span className="rail-brand-mark" aria-hidden="true">
        <span className="rail-brand-glow" />
        <span className="rail-brand-inner" />
        <span className="rail-brand-reticles">
          <span className="rail-brand-reticle-row">
            <i className="rail-brand-corner rail-brand-corner-tl" />
            <i className="rail-brand-corner rail-brand-corner-tr" />
          </span>
          <span className="rail-brand-reticle-row">
            <i className="rail-brand-corner rail-brand-corner-bl" />
            <i className="rail-brand-corner rail-brand-corner-br" />
          </span>
        </span>
        <span className="rail-brand-dot" />
      </span>
      <span className="rail-brand-copy">
        <strong>InsightCut</strong>
        <small key={messageIndex}>{BRAND_MESSAGES[messageIndex]}</small>
      </span>
    </NavLink>

    <nav className="rail-navigation">
      {navigationItems.map(({ to, label, icon: Icon }) => <NavLink
        key={label}
        to={to}
        className={({ isActive }) => `rail-nav-link${isActive ? ' is-active' : ''}`}
      >
        <span><Icon size={19} aria-hidden="true" /></span>
        <small>{label}</small>
      </NavLink>)}
    </nav>
  </aside>
}
