/** Display helpers shared by the views. */

export const PLATFORMS = [
  { value: 'xiaohongshu', label: '小红书', type: 'danger' },
  { value: 'weibo', label: '微博', type: 'warning' },
  { value: 'douyin', label: '抖音', type: 'primary' },
]

const PLATFORM_MAP = Object.fromEntries(PLATFORMS.map((item) => [item.value, item]))

export function platformLabel(value) {
  return PLATFORM_MAP[value]?.label ?? value ?? '-'
}

export function platformType(value) {
  return PLATFORM_MAP[value]?.type ?? 'info'
}

/** The rewrite statuses the backend can write (see services/ai/rewriter.py). */
const STATUS_MAP = {
  READY_TO_PUBLISH: { label: '可发布', type: 'success' },
  NEEDS_REVIEW: { label: '需人工审核', type: 'warning' },
  FAILED: { label: '生成失败', type: 'danger' },
  PENDING: { label: '待生成', type: 'info' },
}

export function statusLabel(value) {
  return STATUS_MAP[value]?.label ?? value ?? '-'
}

export function statusType(value) {
  return STATUS_MAP[value]?.type ?? 'info'
}

const TASK_STATUS_MAP = {
  success: { label: '成功', type: 'success' },
  partial: { label: '部分成功', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
  running: { label: '运行中', type: 'primary' },
}

export function taskStatusLabel(value) {
  return TASK_STATUS_MAP[value]?.label ?? value ?? '-'
}

export function taskStatusType(value) {
  return TASK_STATUS_MAP[value]?.type ?? 'info'
}

export function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '-'
  const number = Number(value)
  if (Number.isNaN(number)) return String(value)
  if (number >= 100000000) return `${(number / 100000000).toFixed(1)}亿`
  if (number >= 10000) return `${(number / 10000).toFixed(1)}万`
  return number.toLocaleString('zh-CN')
}

export function formatDateTime(value) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (part) => String(part).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined) return '-'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`
}

export function confidencePercent(value) {
  if (value === null || value === undefined) return '-'
  return `${Math.round(Number(value) * 100)}%`
}

/** Copy text, reporting whether the Clipboard API was available. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/**
 * Where to load an image from.
 *
 * The downloaded copy is preferred: the provider URLs are signed and expire, so the
 * local file is the durable one. Both are served by the backend, so no CORS and no
 * mixed-content surprises.
 */
export function imageSrc(image) {
  if (!image) return ''
  if (image.local_path) return `/media/${String(image.local_path).replace(/^media\//, '')}`
  return image.url || ''
}

/** Strings a `.env` file may use for "on". */
const TRUE_STRINGS = new Set(['true', '1', 'yes', 'on'])

/**
 * Normalise a stored boolean setting to exactly `'true'` or `'false'`.
 *
 * The values in `.env` are hand-written, so they arrive as `''`, `'True'`, `'1'`…
 * Comparing the raw string against `'true'` in a switch produced two real bugs:
 * `SCHEDULER_ENABLED=True` (capital T) showed the **running** scheduler as disabled,
 * and Element Plus silently rewrote the unmatched values on mount, so a page nobody
 * had touched reported "5 items changed" and offered to save them.
 */
export function normaliseBool(raw) {
  return TRUE_STRINGS.has(String(raw ?? '').trim().toLowerCase()) ? 'true' : 'false'
}
