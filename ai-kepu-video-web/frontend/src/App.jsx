import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from 'react-router'
import { AppErrorBoundary } from './components/AppErrorBoundary'
import { BrandNavigation } from './components/BrandNavigation'
import { GlobalTaskBar } from './components/GlobalTaskBar'
import { ToastViewport } from './components/ToastViewport'
const ExportPage = lazy(() => import('./pages/ExportPage').then(module => ({ default: module.ExportPage })))
const ManuscriptPage = lazy(() => import('./pages/ManuscriptPage').then(module => ({ default: module.ManuscriptPage })))
const ProjectAssetsPage = lazy(() => import('./pages/ProjectAssetsPage').then(module => ({ default: module.ProjectAssetsPage })))
const ProjectAssetDetailPage = lazy(() => import('./pages/ProjectAssetDetailPage').then(module => ({ default: module.ProjectAssetDetailPage })))
const SettingsPage = lazy(() => import('./pages/SettingsPage').then(module => ({ default: module.SettingsPage })))
const TemplatesPage = lazy(() => import('./pages/TemplatesPage').then(module => ({ default: module.TemplatesPage })))
const WorkspacePage = lazy(() => import('./pages/WorkspacePage').then(module => ({ default: module.WorkspacePage })))
const BatchListPage = lazy(() => import('./pages/BatchListPage').then(module => ({ default: module.BatchListPage })))
const BatchDetailPage = lazy(() => import('./pages/BatchDetailPage').then(module => ({ default: module.BatchDetailPage })))
import { getDraft } from './utils/projectDrafts'

export default function App() {
  return (
    <BrowserRouter>
      <AppSurface />
    </BrowserRouter>
  )
}

function AppSurface() {
  const location = useLocation()
  return <AppErrorBoundary resetKey={`${location.pathname}${location.search}`}>
    <div className="app-shell">
      <BrandNavigation />
      <div className="app-main">
        <GlobalTaskBar />
        <Suspense fallback={<main role="status" className="delivery-loading">正在打开页面…</main>}>
        <Routes>
          <Route path="/" element={<Navigate to="/manuscript" replace />} />
          <Route path="/manuscript/:draftId?" element={<ManuscriptPage />} />
          <Route path="/batches" element={<BatchListPage />} />
          <Route path="/batches/:batchId" element={<BatchDetailPage />} />
          <Route path="/workspace/:taskId/*" element={<WorkspacePage />} />
          <Route path="/production/:draftId" element={<ProductionRedirect />} />
          <Route path="/process/:taskId" element={<WorkspaceRedirect />} />
          <Route path="/preview/:taskId" element={<WorkspaceRedirect />} />
          <Route path="/export/:taskId" element={<ExportPage />} />
          <Route path="/assets" element={<ProjectAssetsPage />} />
          <Route path="/assets/:taskId" element={<ProjectAssetDetailPage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/result/:taskId" element={<ResultRedirect />} />
          <Route path="*" element={<Navigate to="/manuscript" replace />} />
        </Routes>
        </Suspense>
      </div>
      <ToastViewport />
    </div>
  </AppErrorBoundary>
}

function ResultRedirect() {
  const { taskId } = useParams()
  return <Navigate to={`/export/${taskId}`} replace />
}

function WorkspaceRedirect() {
  const { taskId } = useParams()
  return <Navigate to={`/workspace/${taskId}`} replace />
}

function ProductionRedirect() {
  const { draftId } = useParams()
  const draft = getDraft(draftId)
  return <Navigate to={draft?.created_task_id ? `/workspace/${draft.created_task_id}` : `/manuscript/${draftId}`} replace />
}
