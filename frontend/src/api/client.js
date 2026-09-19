import axios from 'axios'
import { ElMessage } from 'element-plus'

// Same origin as the API: FastAPI serves this bundle, so there is no CORS setup and
// no base URL to configure between dev and production.
const http = axios.create({
  baseURL: '/api',
  // A DeepSeek analysis run legitimately takes minutes; a 30s default would abort
  // a paid run that is still working.
  timeout: 300000,
})

/** Turn any axios failure into one sentence a human can act on. */
export function errorMessage(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && Array.isArray(detail.errors)) return detail.errors.join('；')
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  if (error?.code === 'ECONNABORTED') return '请求超时：后端仍在处理，请稍后刷新任务列表确认结果'
  if (error?.message) return error.message
  return '请求失败'
}

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    // The caller may still want to show the error inline; surface it once here so
    // no view has to remember to.
    if (!error?.config?.silent) ElMessage.error(errorMessage(error))
    return Promise.reject(error)
  },
)

export const api = {
  // --- read-only (never billed) -----------------------------------------------
  status: (probe = true) => http.get('/system/status', { params: { probe } }),
  stats: (today = true) => http.get('/system/stats', { params: { today } }),
  stored: (params) => http.get('/hot/stored', { params }),
  topics: (params) => http.get('/hot/topics', { params }),
  topic: (id, includeRaw = false) =>
    http.get(`/hot/topics/${id}`, { params: { include_raw: includeRaw } }),
  analyses: (params) => http.get('/analysis', { params }),
  analysis: (hotContentId) => http.get(`/analysis/${hotContentId}`),
  profile: () => http.get('/analysis/profile/current'),
  rewrites: (params) => http.get('/rewrite', { params }),
  rewrite: (hotContentId) => http.get(`/rewrite/${hotContentId}`),
  tasks: (params) => http.get('/pipeline/tasks', { params }),
  schedule: () => http.get('/pipeline/schedule'),
  notificationStatus: () => http.get('/notification/status'),
  settings: () => http.get('/settings'),

  // --- billed or side-effecting (always behind an explicit confirmation) ------
  liveHot: (params) => http.get('/hot', { params }),
  collect: (params) => http.post('/hot/collect', null, { params }),
  // Keyword search: one billed TikHub call per platform, and it returns real posts
  // (with images) rather than ranking keywords.
  searchTopic: (payload) => http.post('/hot/search', payload),
  runAnalysis: (params) => http.post('/analysis/run', null, { params }),
  /** Analyse exactly one item (the drawer's button). Bypasses the interest filter. */
  runAnalysisOne: (hotContentId) =>
    http.post('/analysis/run', null, { params: { hot_content_id: hotContentId } }),
  runRewrite: (params) => http.post('/rewrite/run', null, { params }),
  /** Rewrite exactly one item; requires an analysis to exist. */
  runRewriteOne: (hotContentId) =>
    http.post('/rewrite/run', null, { params: { hot_content_id: hotContentId } }),
  runPipeline: (params) => http.post('/pipeline/run', null, { params }),
  sendNotificationTest: () => http.post('/notification/test'),

  // --- configuration ----------------------------------------------------------
  updateSettings: (values) => http.put('/settings', { values }),
  updateProfile: (profile) => http.put('/settings/profile', profile),

  // --- domain focus (Phase 10) ------------------------------------------------
  /** Run the configured domain-keyword searches (keywords x platforms billed calls). */
  runWatch: () => http.post('/hot/watch'),
  /** Free: what is waiting to be analysed, and what a run would cost. */
  pendingAnalysis: () => http.get('/hot/pending'),

  // --- README -> promotion copy (Phase 10; spends DeepSeek tokens) ------------
  promos: (params) => http.get('/promo/readme', { params }),
  promo: (id) => http.get(`/promo/readme/${id}`),
  createPromo: (payload) => http.post('/promo/readme', payload),

  // --- 图片二创 (Phase 11; billed PER IMAGE, ~15x a text rewrite) --------------
  /** Free: price per image and whether this item has usable local material. */
  imageEstimate: (hotContentId) =>
    http.get('/image/estimate', { params: { hot_content_id: hotContentId } }),
  imageGenerations: (params) => http.get('/image/generations', { params }),
  generateImage: (payload) => http.post('/image/generate', payload),

  // --- QQ push (Phase 12; sends content OUT) -----------------------------------
  /** dry_run=true composes and returns the two messages without sending anything. */
  pushItem: (hotContentId, { dryRun = true, order = 'rewrite_first' } = {}) =>
    http.post('/notification/push-item', {
      hot_content_id: hotContentId,
      dry_run: dryRun,
      order,
    }),

  // --- 知识科普 + 标签球 (Phase 13) --------------------------------------------
  /** Free: the stored vocabulary (generated once when empty, then reused). */
  knowledgeTags: (params) => http.get('/knowledge/tags', { params }),
  /** Billed: one DeepSeek call to regenerate the vocabulary. */
  refreshKnowledgeTags: (extra = '') => http.post('/knowledge/tags/refresh', { extra }),
  knowledgeArticles: (params) => http.get('/knowledge/articles', { params }),
  knowledgeArticle: (id) => http.get(`/knowledge/articles/${id}`),
  /** Billed: 1-2 DeepSeek calls + (by default) one image search. */
  createKnowledgeArticle: (payload) => http.post('/knowledge/articles', payload),
  /**
   * Same as above, but streams per-step progress as NDJSON.
   *
   * `fetch` rather than axios: reading a stream incrementally needs the raw body reader,
   * and axios buffers the whole response. `onEvent` is called for every line.
   */
  async createKnowledgeArticleStream(payload, onEvent, signal) {
    const response = await fetch('/api/knowledge/articles/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    })
    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try {
        const body = await response.json()
        detail = body?.detail || detail
      } catch {
        // keep the status line
      }
      throw new Error(detail)
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // NDJSON: one JSON object per line; the last line may be incomplete.
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try {
          onEvent(JSON.parse(trimmed))
        } catch {
          // A malformed line is skipped rather than aborting a paid generation.
        }
      }
    }
    if (buffer.trim()) {
      try {
        onEvent(JSON.parse(buffer.trim()))
      } catch {
        // ignore
      }
    }
  },
  /** Billed: one TikHub image search, returns several images. */
  searchKnowledgeImages: (payload) => http.post('/knowledge/images/search', payload),
}

export default http
