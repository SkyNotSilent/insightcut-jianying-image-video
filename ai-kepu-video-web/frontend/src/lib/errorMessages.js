const ERROR_COPY = Object.freeze({
  auth: {
    title: 'API 凭证不可用',
    action: '请打开 API 配置，更新凭证后再重试。',
  },
  rate_limit: {
    title: '服务请求过于频繁',
    action: '请稍后重试，已完成的内容不会丢失。',
  },
  timeout: {
    title: '生成服务响应超时',
    action: '请检查网络后重试当前项。',
  },
  provider_error: {
    title: '生成服务暂时异常',
    action: '请稍后重试当前项，其他素材保持不变。',
  },
  content_policy: {
    title: '画面描述未通过内容检查',
    action: '请修改当前分镜的生图提示词后，再重新生成这张图片。',
  },
  network: {
    title: '无法连接生成服务',
    action: '请检查网络连接，恢复后再重试。',
  },
  disk: {
    title: '本地文件写入失败',
    action: '请检查磁盘空间和目录权限后再试。',
  },
  config_missing: {
    title: '生成服务尚未配置完整',
    action: '请先完成 API 配置，再继续当前项目。',
  },
  conflict: {
    title: '内容版本已变化',
    action: '请刷新最新状态，确认后再重试。',
  },
  cancelled: {
    title: '本次操作已取消',
    action: '已完成的内容已保留，需要时可以重新发起。',
  },
  unknown: {
    title: '当前操作未完成',
    action: '请重试当前操作；若仍失败，请检查 API 配置。',
  },
})

const ERROR_CODES = new Set(Object.keys(ERROR_COPY))

function numberOrNull(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function sourceCode(source) {
  if (typeof source === 'string' && ERROR_CODES.has(source)) return source
  if (!source || typeof source !== 'object') return ''
  const candidates = [
    source.error_code,
    source.code,
    source.error?.code,
    source.detail?.code,
    source.response?.data?.error_code,
    source.response?.data?.detail?.code,
  ]
  return candidates.find(code => ERROR_CODES.has(code)) || ''
}

function statusCode(source) {
  if (!source || typeof source !== 'object') return null
  return numberOrNull(
    source.status
      ?? source.http_status
      ?? source.error_meta?.http_status
      ?? source.response?.status,
  )
}

function inferCode(source) {
  const explicit = sourceCode(source)
  if (explicit) return explicit
  if (source?.kind === 'network') return 'network'
  if (source?.kind === 'cancelled') return 'cancelled'
  const status = statusCode(source)
  if (status === 401 || status === 403) return 'auth'
  if (status === 408 || status === 504) return 'timeout'
  if (status === 409) return 'conflict'
  if (status === 429) return 'rate_limit'
  if (status !== null && status >= 500) return 'provider_error'
  return 'unknown'
}

function sourceMetadata(source) {
  if (!source || typeof source !== 'object') return {}
  return source.error_meta
    || source.meta
    || source.response?.data?.error_meta
    || source.response?.data?.detail?.error_meta
    || {}
}

export function getErrorPresentation(source, { fallbackCode = 'unknown' } = {}) {
  const inferred = inferCode(source)
  const code = inferred === 'unknown' && ERROR_CODES.has(fallbackCode) ? fallbackCode : inferred
  const copy = ERROR_COPY[code] || ERROR_COPY.unknown
  const metadata = sourceMetadata(source)
  const retryAfter = numberOrNull(metadata.retry_after_seconds)
  const action = code === 'rate_limit' && retryAfter !== null
    ? `请等待 ${Math.max(0, Math.ceil(retryAfter))} 秒后重试，已完成的内容不会丢失。`
    : copy.action
  return {
    code,
    title: copy.title,
    action,
    retryable: typeof metadata.retryable === 'boolean'
      ? metadata.retryable
      : !['auth', 'content_policy', 'disk', 'config_missing', 'conflict', 'cancelled'].includes(code),
  }
}

export function errorToastMessage(source, options) {
  const presentation = getErrorPresentation(source, options)
  return `${presentation.title}；${presentation.action}`
}
