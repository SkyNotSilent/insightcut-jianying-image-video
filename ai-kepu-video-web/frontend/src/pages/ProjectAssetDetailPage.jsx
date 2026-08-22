import { ArrowLeft, Download, Image as ImageIcon, Mic2, Subtitles, WandSparkles } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'

import { getAssetsDownloadUrl, getTaskAssets, getTaskStatus } from '../api/task'

const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'image', label: '图片' },
  { id: 'audio', label: '配音' },
  { id: 'subtitle', label: '字幕' },
]

export function ProjectAssetDetailPage() {
  const { taskId } = useParams()
  const [task, setTask] = useState(null)
  const [assets, setAssets] = useState([])
  const [filter, setFilter] = useState('all')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    Promise.all([getTaskStatus(taskId, { silent: true }), getTaskAssets(taskId)])
      .then(([taskResult, assetResult]) => {
        if (!alive) return
        setTask(taskResult)
        setAssets(Array.isArray(assetResult) ? assetResult : [])
      })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [taskId])

  const visible = useMemo(() => filter === 'all' ? assets : assets.filter(asset => asset.asset_type === filter), [assets, filter])
  const Icon = ({ type }) => type === 'audio' ? <Mic2 size={18} /> : type === 'subtitle' ? <Subtitles size={18} /> : <ImageIcon size={18} />

  return <main className="catalog-page asset-detail-page">
    <header className="catalog-heading">
      <div><Link className="back-link" to="/assets"><ArrowLeft size={16} /> 全部项目</Link><h1>{task?.name || task?.theme || '项目素材'}</h1><p>查看素材来源、所属分镜与历史版本；下载不会触发任何模型调用。</p></div>
      <div className="catalog-actions"><Link className="button button-secondary" to={`/workspace/${taskId}`}><WandSparkles size={16} /> 进入工作台</Link><a className="button button-primary" href={getAssetsDownloadUrl(taskId, 'all')}><Download size={16} /> 下载素材包</a></div>
    </header>
    <nav className="asset-filter-tabs" aria-label="素材类型">
      {FILTERS.map(item => <button type="button" key={item.id} className={filter === item.id ? 'is-active' : ''} onClick={() => setFilter(item.id)}>{item.label}<span>{item.id === 'all' ? assets.length : assets.filter(asset => asset.asset_type === item.id).length}</span></button>)}
    </nav>
    {loading ? <div className="catalog-loading">正在整理项目素材…</div> : visible.length ? <section className="asset-library-grid">
      {visible.map(asset => <article className={`asset-library-card type-${asset.asset_type}`} key={asset.asset_id}>
        <div className="asset-library-preview">
          {asset.asset_type === 'image' && (asset.url || asset.file_url) ? <img src={asset.url || asset.file_url} alt="" /> : <Icon type={asset.asset_type} />}
        </div>
        <div className="asset-library-copy"><span><Icon type={asset.asset_type} /> {asset.label}</span><strong>{asset.segment_index == null ? '项目素材' : `分镜 ${asset.segment_index + 1}`}</strong><small>{asset.source} · {asset.created_at || '历史记录'}</small></div>
        {asset.download_url ? <a className="icon-button" href={asset.download_url} aria-label="下载素材"><Download size={16} /></a> : null}
      </article>)}
    </section> : <section className="catalog-empty"><h2>当前筛选下没有素材</h2><p>生成成功的图片、配音和字幕会保留在这里。</p></section>}
  </main>
}
