/**
 * 任务 API
 * 创建任务、查询任务状态
 */

import request from './request'

/**
 * 获取可用的 TTS 音色列表
 * @returns {Promise<Array>} 音色列表
 */
export function getVoices(params = {}) {
  return request({
    url: '/ai/native/video/kepu/voices',
    method: 'get',
    params
  })
}

export function updateVoiceAvailability(voiceKeys) {
  return request({
    url: '/ai/native/video/kepu/voices/availability',
    method: 'put',
    data: { voice_keys: voiceKeys }
  })
}

export function previewVoice(data, { silent = false } = {}) {
  return request({
    url: '/ai/native/video/kepu/voices/preview',
    method: 'post',
    data,
    suppressToast: silent,
    timeout: 120000
  })
}

export function getVoiceClones(params = {}) {
  return request({
    url: '/ai/native/video/kepu/voice-clones',
    method: 'get',
    params
  })
}

export function createVoiceClone({ name, consentConfirmed, file }) {
  const formData = new FormData()
  formData.append('name', name)
  formData.append('consent_confirmed', String(Boolean(consentConfirmed)))
  formData.append('file', file)
  return request({
    url: '/ai/native/video/kepu/voice-clones',
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000
  })
}

export function updateVoiceClone(cloneId, data) {
  return request({
    url: `/ai/native/video/kepu/voice-clones/${cloneId}`,
    method: 'patch',
    data
  })
}

export function replaceVoiceCloneReference(cloneId, file) {
  const formData = new FormData()
  formData.append('file', file)
  return request({
    url: `/ai/native/video/kepu/voice-clones/${cloneId}/reference`,
    method: 'put',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 120000
  })
}

export function previewVoiceClone(cloneId, data = {}) {
  return request({
    url: `/ai/native/video/kepu/voice-clones/${cloneId}/preview`,
    method: 'post',
    data,
    timeout: 120000
  })
}

export function deleteVoiceClone(cloneId) {
  return request({
    url: `/ai/native/video/kepu/voice-clones/${cloneId}`,
    method: 'delete'
  })
}

export function getConfig() {
  return request({
    url: '/ai/native/video/kepu/config',
    method: 'get'
  })
}

export function updateConfig(data) {
  return request({
    url: '/ai/native/video/kepu/config',
    method: 'put',
    data
  })
}

export function getConfigReadiness({ voiceType, signal } = {}) {
  return request({
    url: '/ai/native/video/kepu/config/readiness',
    method: 'get',
    params: voiceType ? { voice_type: voiceType } : {},
    suppressToast: true,
    signal,
    timeout: 10000,
  })
}

export function fetchConfigModels(data) {
  return request({
    url: '/ai/native/video/kepu/config/models',
    method: 'post',
    data
  })
}

export const getLlmProviders = () => request({
  url: '/ai/native/video/kepu/config/llm-providers', method: 'get'
})

export const getLlmProviderModels = providerId => request({
  url: `/ai/native/video/kepu/config/llm-providers/${providerId}/models`, method: 'get'
})

export const refreshLlmProviderModels = (providerId, data) => request({
  url: `/ai/native/video/kepu/config/llm-providers/${providerId}/models/refresh`,
  method: 'post', data, timeout: 30000
})

export function testTtsConfig(data) {
  return request({
    url: '/ai/native/video/kepu/config/test-tts',
    method: 'post',
    data
  })
}

export function extractDocumentText(file) {
  const formData = new FormData()
  formData.append('file', file)
  return request({
    url: '/ai/native/video/kepu/documents/extract-text',
    method: 'post',
    data: formData,
    headers: {
      'Content-Type': 'multipart/form-data'
    },
    timeout: 120000
  })
}

export function getRenderConfig() {
  return request({
    url: '/ai/native/video/kepu/render-config',
    method: 'get'
  })
}

/**
 * 创建视频生成任务
 * @param {Object} data - 任务参数
 * @param {string} data.theme - 剧本文稿（后端兼容字段名，最多 5000 字）
 * @param {string} data.style - 文章风格
 * @param {number} data.length - 兼容旧接口的长度参数，文稿生产传 0
 * @param {string} data.visual_style - 画面风格（写实风格/电影级/油彩画/毛毡风）
 * @param {string} data.ratio - 画幅比例（16:9/9:16）
 * @param {string} data.voice_type - TTS 音色 ID（可选）
 * @returns {Promise<{task_id: string, status: string}>}
 */
export function createTask(data) {
  return request({
    url: '/ai/native/video/kepu/tasks',
    method: 'post',
    data
  })
}

/**
 * 使用本地上传图片创建可编辑任务
 * @param {FormData} formData - images/style/voice_type/name
 * @returns {Promise<{task_id: string, status: string}>}
 */
export function createTaskFromImages(formData) {
  return request({
    url: '/ai/native/video/kepu/tasks/create-from-images',
    method: 'post',
    data: formData,
    headers: {
      'Content-Type': 'multipart/form-data'
    },
    timeout: 120000
  })
}

/**
 * 查询任务状态
 * @param {string} taskId - 任务ID
 * @returns {Promise<Object>} 任务详情
 */
export function getTaskStatus(taskId, { silent = false, signal } = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}`,
    method: 'get',
    suppressToast: silent,
    signal,
  })
}

export function getTaskWorkspace(taskId, { silent = false, signal } = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/workspace`,
    method: 'get',
    suppressToast: silent,
    signal,
  })
}

export function updateTaskWorkspaceSettings(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/settings`,
    method: 'patch',
    data
  })
}

export function generateTaskWorkspaceAssets(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/generate-assets`,
    method: 'post',
    data
  })
}

export function resegmentTaskWorkspace(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/resegment`,
    method: 'post',
    data
  })
}

export function resumeTask(taskId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/resume`,
    method: 'post'
  })
}

export function retryTaskAssets(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/retry-assets`,
    method: 'post',
    data
  })
}

export function regenerateSegmentPrompt(taskId, segmentIndex, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}/regenerate-prompt`,
    method: 'post',
    data,
  })
}

export function finalizeTaskWorkspace(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/finalize`,
    method: 'post',
    data
  })
}

/**
 * 删除任务
 * @param {string} taskId - 任务ID
 * @returns {Promise<{message: string}>}
 */
export function deleteTask(taskId, { deleteFiles = true } = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}`,
    method: 'delete',
    params: { delete_files: deleteFiles }
  })
}

export function cancelTask(taskId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/cancel`,
    method: 'post',
  })
}

/**
 * 获取任务段落列表
 * @param {string} taskId - 任务ID
 * @returns {Promise<Array>} 段落列表
 */
export function getSegments(taskId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments`,
    method: 'get'
  })
}

export function getTaskRenderConfig(taskId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/render-config`,
    method: 'get'
  })
}

export function renderPreview(taskId, segmentIndex = null) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/preview-render`,
    method: 'post',
    params: segmentIndex === null ? {} : { segment_index: segmentIndex },
    timeout: 300000
  })
}

export function getExportState(taskId, { silent = false, signal } = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/export-state`,
    method: 'get',
    suppressToast: silent,
    signal,
  })
}

export function createExport(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/exports`,
    method: 'post',
    data
  })
}

export function getExportJob(taskId, jobId, { silent = false, signal } = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/exports/${jobId}`,
    method: 'get',
    suppressToast: silent,
    signal,
  })
}

export function cancelExportJob(taskId, jobId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/exports/${jobId}/cancel`,
    method: 'post',
  })
}

export function selectDraftFolder(taskId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/draft-folder/select`,
    method: 'post',
    timeout: 300000
  })
}

export function validateDraftFolder(taskId, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/draft-folder/validate`,
    method: 'post',
    data
  })
}

export function getTaskAssets(taskId, params = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/assets`,
    method: 'get',
    params
  })
}

export function selectSegmentImage(taskId, segmentIndex, assetId, snapshotKey = '') {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}/select-image`,
    method: 'post',
    data: { asset_id: assetId, snapshot_key: snapshotKey }
  })
}

export function getAssetLibrary(taskId, params = {}) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/asset-library`,
    method: 'get',
    params,
  })
}

export function selectSegmentAsset(taskId, segmentIndex, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}/select-asset`,
    method: 'post',
    data,
  })
}

export function getSubtitleUrl(taskId) {
  return `/ai/native/video/kepu/tasks/${taskId}/subtitle.srt`
}

export function getAssetsDownloadUrl(taskId, type = 'all') {
  return `/ai/native/video/kepu/tasks/${taskId}/assets/download?type=${encodeURIComponent(type)}`
}

export function getMaterialsDownloadUrl(taskId, snapshotKey) {
  return `/ai/native/video/kepu/tasks/${taskId}/download-materials?snapshot_key=${encodeURIComponent(snapshotKey || '')}`
}

/**
 * 更新段落内容
 * @param {string} taskId - 任务ID
 * @param {number} segmentIndex - 段落索引
 * @param {Object} data - 更新数据
 * @param {string} data.text - 新文案（可选）
 * @param {string} data.image_url - 新图片URL（可选）
 * @param {string} data.audio_url - 新音频URL（可选）
 * @returns {Promise<{message: string}>}
 */
export function updateSegment(taskId, segmentIndex, data) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}`,
    method: 'put',
    data
  })
}

/**
 * 重新生成段落图片
 * @param {string} taskId - 任务ID
 * @param {number} segmentIndex - 段落索引
 * @returns {Promise<{message: string, image_path: string, image_url: string}>}
 */
export function regenerateImage(taskId, segmentIndex) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}/regenerate-image`,
    method: 'post',
    timeout: 180000  // 3分钟超时
  })
}

/**
 * 上传自定义图片
 * @param {string} taskId - 任务ID
 * @param {number} segmentIndex - 段落索引
 * @param {File} file - 图片文件
 * @returns {Promise<{message: string, image_path: string, image_url: string}>}
 */
export function uploadImage(taskId, segmentIndex, file) {
  const formData = new FormData()
  formData.append('file', file)
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}/upload-image`,
    method: 'post',
    data: formData,
    headers: {
      'Content-Type': 'multipart/form-data'
    },
    timeout: 60000  // 1分钟超时
  })
}

/**
 * 重新生成段落音频
 * @param {string} taskId - 任务ID
 * @param {number} segmentIndex - 段落索引
 * @param {string} voiceType - TTS 音色 ID（可选）
 * @returns {Promise<{message: string, audio_path: string}>}
 */
export function regenerateAudio(taskId, segmentIndex, voiceOrPayload = null, ttsOptions = null) {
  const data = typeof voiceOrPayload === 'object' && voiceOrPayload !== null
    ? voiceOrPayload
    : (voiceOrPayload || ttsOptions)
      ? { voice_type: voiceOrPayload || null, tts_options: ttsOptions || null }
      : undefined
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/segments/${segmentIndex}/regenerate-audio`,
    method: 'post',
    data,
    timeout: 120000  // 2分钟超时
  })
}

/**
 * 重新构建草稿和视频
 * @param {string} taskId - 任务ID
 * @returns {Promise<{message: string, draft_path: string, video_path: string}>}
 */
export function rebuildDraft(taskId) {
  return request({
    url: `/ai/native/video/kepu/tasks/${taskId}/rebuild`,
    method: 'post'
  })
}

export function listTasks(status, limit = 20, offset = 0) {
  return request({
    url: '/ai/native/video/kepu/tasks',
    method: 'get',
    params: { status, limit, offset }
  })
}

export function getTaskActivity({ signal } = {}) {
  return request({
    url: '/ai/native/video/kepu/activity/tasks',
    method: 'get',
    suppressToast: true,
    signal,
  })
}

export function listProductionTemplates() {
  return request({ url: '/ai/native/video/kepu/templates', method: 'get' })
}

export function createProductionTemplate(data) {
  return request({ url: '/ai/native/video/kepu/templates', method: 'post', data })
}

export function updateProductionTemplate(templateId, data) {
  return request({ url: `/ai/native/video/kepu/templates/${templateId}`, method: 'patch', data })
}

export function deleteProductionTemplate(templateId) {
  return request({ url: `/ai/native/video/kepu/templates/${templateId}`, method: 'delete' })
}
