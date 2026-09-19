<script setup>
/**
 * 项目推广：把一份 README 变成小红书 / 微博 / 抖音三个平台的推广文案。
 *
 * 设计要点（与后端一致）：
 * - 一次生成三份，**花费 DeepSeek token**，所以按钮后面必须有一次费用确认。
 * - README 只送结构化节选，被截断时页面显示"已节选（x/y 字符）"，不让人误以为
 *   模型看完了全文。
 * - 页面显式展示模型自报的"未知信息"，因为那些正是它没敢编造的地方。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import CostConfirmDialog from '@/components/CostConfirmDialog.vue'
import { api } from '@/api/client'
import { copyText, formatDateTime } from '@/api/format'

const form = reactive({
  mode: 'text',
  text: '',
  path: '',
  url: '',
  project_name: '',
  extra_note: '',
  /** Optional 小红书 note link used as a style reference (1 billed TikHub call). */
  styleUrl: '',
})

const generating = ref(false)
const confirmVisible = ref(false)
const active = ref(null)
const history = ref([])
const historyLoading = ref(false)
const activeTab = ref('xiaohongshu')

const MODES = [
  { value: 'text', label: '粘贴内容' },
  { value: 'path', label: '本地文件路径' },
  { value: 'url', label: '网址' },
]

const sourceReady = computed(() => {
  if (form.mode === 'text') return form.text.trim().length > 0
  if (form.mode === 'path') return form.path.trim().length > 0
  return /^https?:\/\//.test(form.url.trim())
})

const versions = computed(() => active.value?.versions ?? {})
const xhs = computed(() => versions.value.xiaohongshu ?? {})
const weibo = computed(() => versions.value.weibo ?? {})
const douyin = computed(() => versions.value.douyin ?? {})

const drafts = computed(() => {
  const value = active.value
  if (!value) return {}
  const tags = (list) =>
    (list || []).map((tag) => (tag.startsWith('#') ? tag : `#${tag}#`)).join(' ')
  const v = value.versions ?? {}
  return {
    xiaohongshu: [
      v.xiaohongshu?.title,
      v.xiaohongshu?.content,
      v.xiaohongshu?.ending,
      tags(v.xiaohongshu?.hashtags),
    ]
      .filter(Boolean)
      .join('\n\n'),
    weibo: [v.weibo?.opening, v.weibo?.content, tags(v.weibo?.hashtags)]
      .filter(Boolean)
      .join('\n\n'),
    douyin: [
      v.douyin?.hook ? `【开场钩子】${v.douyin.hook}` : '',
      v.douyin?.script,
      (v.douyin?.scenes || []).length
        ? `【分镜建议】\n${(v.douyin.scenes || []).map((s, i) => `${i + 1}. ${s}`).join('\n')}`
        : '',
      v.douyin?.cues ? `【字幕】${v.douyin.cues}` : '',
      v.douyin?.cta ? `【结尾引导】${v.douyin.cta}` : '',
    ]
      .filter(Boolean)
      .join('\n\n'),
  }
})

const costNote = computed(() => {
  const lines = [
    '将调用 DeepSeek 生成三平台文案。README 只送结构化节选（上限约 12000 字符），' +
      '因此费用可预期；被节选时页面会明确标注。',
  ]
  if (form.styleUrl.trim()) {
    lines.push(
      '另外会用 1 次 TikHub 调用读取风格参考笔记（约 $0.008）。' +
        '链接解析本身免费——链接无效时会在调用模型之前失败，不产生费用。',
    )
  }
  return lines.join('\n')
})

async function loadHistory() {
  historyLoading.value = true
  try {
    const response = await api.promos({ limit: 20 })
    history.value = response.items ?? []
  } catch {
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

function ask() {
  if (!sourceReady.value) {
    ElMessage.warning(
      form.mode === 'text' ? '请先粘贴 README 内容' : form.mode === 'path' ? '请填写文件路径' : '请填写 http(s) 网址',
    )
    return
  }
  confirmVisible.value = true
}

async function generate() {
  generating.value = true
  try {
    const payload = { project_name: form.project_name, extra_note: form.extra_note }
    if (form.mode === 'text') payload.text = form.text
    else if (form.mode === 'path') payload.path = form.path.trim()
    else payload.url = form.url.trim()
    if (form.styleUrl.trim()) payload.style_reference_url = form.styleUrl.trim()

    const response = await api.createPromo(payload)
    active.value = response.promo ?? null
    if (active.value) {
      activeTab.value = 'xiaohongshu'
      const truncated = active.value.truncated
      ElMessage.success(
        truncated
          ? `已生成（README 已节选：原文 ${active.value.readme_chars} 字符 → 送 ${active.value.sent_chars} 字符）`
          : '已生成三平台文案',
      )
    }
    confirmVisible.value = false
    await loadHistory()
  } catch {
    // The interceptor reported it; keep the dialog open so the input is not lost.
  } finally {
    generating.value = false
  }
}

async function copy(platform) {
  const text = drafts.value[platform]
  if (!text) {
    ElMessage.warning('该平台暂无内容')
    return
  }
  if (await copyText(text)) ElMessage.success('已复制')
  else ElMessage.warning('浏览器拒绝了剪贴板访问，请手动选中')
}

async function openHistory(id) {
  try {
    const response = await api.promo(id)
    active.value = response.item
    activeTab.value = 'xiaohongshu'
  } catch {
    // Reported by the interceptor.
  }
}

onMounted(loadHistory)
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">项目推广</h2>
        <p class="sh-page-subtitle">
          把开源项目的 README 转成小红书 / 微博 / 抖音推广文案。只依据 README 里真实存在的内容，不编造数据与经历。
        </p>
      </div>
      <el-button :loading="historyLoading" @click="loadHistory">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
    </div>

    <el-card shadow="never" class="sh-card">
      <template #header>输入 README</template>
      <el-radio-group v-model="form.mode" style="margin-bottom: 12px">
        <el-radio-button v-for="mode in MODES" :key="mode.value" :value="mode.value">
          {{ mode.label }}
        </el-radio-button>
      </el-radio-group>

      <el-input
        v-if="form.mode === 'text'"
        v-model="form.text"
        type="textarea"
        :rows="12"
        placeholder="把 README 的 Markdown 内容粘贴到这里"
        show-word-limit
      />
      <el-input
        v-else-if="form.mode === 'path'"
        v-model="form.path"
        placeholder="例如 D:\myproject\README.md（只接受 .md/.markdown/.txt/.rst，或名为 README 的文件）"
      />
      <el-input v-else v-model="form.url" placeholder="https://raw.githubusercontent.com/user/repo/main/README.md" />
      <el-alert
        v-if="form.mode === 'url'"
        style="margin-top: 10px"
        type="warning"
        :closable="false"
        show-icon
        title="这里要的是 README 文件的原始内容地址，不是网页"
        description="小红书/知乎等分享链接、GitHub 仓库主页都会返回网页（常需登录），系统会在调用模型之前直接拒绝并说明原因，不会产生费用。正确做法：用 raw 文件链接，或切到「粘贴内容」。"
      />

      <el-form label-width="90px" style="margin-top: 14px">
        <el-form-item label="项目名">
          <el-input v-model="form.project_name" placeholder="可留空，默认取 README 的一级标题" />
        </el-form-item>
        <el-form-item label="补充要求">
          <el-input
            v-model="form.extra_note"
            placeholder="例如：读者是刚学编程的大一学生 / 重点讲它能省什么时间"
          />
        </el-form-item>
        <el-form-item label="风格参考">
          <el-input
            v-model="form.styleUrl"
            placeholder="可选：粘贴一篇小红书笔记的分享链接，学它的形式（标题长度/分段/语气/标签）"
          />
          <div class="sh-muted" style="margin-top: 4px">
            只学**形式**，不抄内容——提示词里明确禁止复制它的句子与经历。解析链接免费，读取笔记 1 次 TikHub 调用。
          </div>
        </el-form-item>
      </el-form>

      <el-button type="primary" :disabled="!sourceReady" @click="ask">
        <el-icon><MagicStick /></el-icon>&nbsp;生成三平台文案
      </el-button>
      <div class="sh-muted" style="margin-top: 6px">点击后会先弹出费用确认。</div>
    </el-card>

    <el-card v-if="active" shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <span>{{ active.project_name || '未命名项目' }}</span>
          <el-tag v-if="active.truncated" type="warning" size="small">
            README 已节选（{{ active.readme_chars }} → {{ active.sent_chars }} 字符）
          </el-tag>
          <div class="sh-spacer" />
          <span class="sh-muted">
            {{ active.model }} · token {{ active.tokens?.prompt ?? 0 }}/{{ active.tokens?.completion ?? 0 }}
          </span>
        </div>
      </template>

      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="一句话定位">{{ active.one_liner || '-' }}</el-descriptions-item>
        <el-descriptions-item label="封面大字">
          <el-tag type="danger" effect="dark">{{ active.cover_text || '-' }}</el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <el-alert
        v-if="active.style_reference?.title"
        style="margin-top: 12px"
        type="success"
        :closable="false"
        show-icon
        title="本次使用了风格参考笔记（只学形式，未复制内容）"
        :description="`参考：${active.style_reference.title}（标题 ${active.style_reference.title_length} 字 / 正文 ${active.style_reference.body_length} 字 / ${active.style_reference.paragraphs} 段 / ${active.style_reference.image_count} 图 / ${(active.style_reference.hashtags || []).length} 个标签）${active.style_reference.author ? ' · 作者 ' + active.style_reference.author : ''}`"
      />

      <el-alert
        v-if="active.unknowns?.length"
        style="margin-top: 12px"
        type="warning"
        :closable="false"
        show-icon
        title="模型无法从 README 确定的信息（它没有编造，需要你补充）"
        :description="active.unknowns.join('；')"
      />

      <div v-if="active.source_points?.length" style="margin-top: 12px">
        <div class="sh-muted" style="margin-bottom: 6px">文案依据的 README 要点（可核对）：</div>
        <el-tag
          v-for="point in active.source_points"
          :key="point"
          size="small"
          effect="plain"
          style="margin: 0 6px 6px 0"
        >
          {{ point }}
        </el-tag>
      </div>

      <div v-if="active.image_ideas?.length" style="margin-top: 10px">
        <div class="sh-muted" style="margin-bottom: 6px">配图建议（系统不会生成图片）：</div>
        <ol style="margin: 0; padding-left: 20px; line-height: 1.8">
          <li v-for="idea in active.image_ideas" :key="idea">{{ idea }}</li>
        </ol>
      </div>

      <el-divider />

      <el-tabs v-model="activeTab">
        <el-tab-pane label="小红书" name="xiaohongshu">
          <div class="sh-copy-block">
            <h3 style="margin: 0 0 8px">{{ xhs.title || '（无标题）' }}</h3>
            <div class="sh-draft">{{ xhs.content || '（无正文）' }}</div>
            <div v-if="xhs.ending" class="sh-draft" style="margin-top: 10px">{{ xhs.ending }}</div>
            <div style="margin-top: 10px">
              <el-tag v-for="tag in xhs.hashtags || []" :key="tag" size="small" effect="plain" style="margin-right: 4px">
                {{ tag }}
              </el-tag>
            </div>
          </div>
          <el-button type="primary" @click="copy('xiaohongshu')">复制小红书文案</el-button>
        </el-tab-pane>

        <el-tab-pane label="微博" name="weibo">
          <div class="sh-copy-block">
            <div v-if="weibo.opening" class="sh-draft" style="font-weight: 600">{{ weibo.opening }}</div>
            <div class="sh-draft" style="margin-top: 8px">{{ weibo.content || '（无正文）' }}</div>
            <div style="margin-top: 10px">
              <el-tag v-for="tag in weibo.hashtags || []" :key="tag" size="small" effect="plain" style="margin-right: 4px">
                {{ tag }}
              </el-tag>
            </div>
          </div>
          <el-button type="primary" @click="copy('weibo')">复制微博文案</el-button>
        </el-tab-pane>

        <el-tab-pane label="抖音" name="douyin">
          <div class="sh-copy-block">
            <div class="sh-draft"><strong>开场钩子：</strong>{{ douyin.hook || '（无）' }}</div>
            <el-divider style="margin: 10px 0" />
            <div class="sh-draft">{{ douyin.script || '（无口播脚本）' }}</div>
            <template v-if="douyin.scenes?.length">
              <el-divider style="margin: 10px 0" />
              <div style="font-weight: 600; margin-bottom: 6px">分镜建议</div>
              <ol style="margin: 0; padding-left: 20px; line-height: 1.8">
                <li v-for="(scene, index) in douyin.scenes" :key="index">{{ scene }}</li>
              </ol>
            </template>
            <div v-if="douyin.cues" style="margin-top: 10px"><strong>字幕：</strong>{{ douyin.cues }}</div>
            <div v-if="douyin.cta" style="margin-top: 10px"><strong>结尾引导：</strong>{{ douyin.cta }}</div>
          </div>
          <el-button type="primary" @click="copy('douyin')">复制抖音脚本</el-button>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <el-card shadow="never" class="sh-card">
      <template #header>历史记录</template>
      <el-table v-loading="historyLoading" :data="history" size="small">
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column label="项目" min-width="180">
          <template #default="{ row }">
            <el-link type="primary" @click="openHistory(row.id)">{{ row.project_name || '未命名' }}</el-link>
            <div class="sh-muted">{{ row.one_liner }}</div>
          </template>
        </el-table-column>
        <el-table-column label="小红书标题" min-width="160">
          <template #default="{ row }">
            {{ row.versions?.xiaohongshu?.title || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.source_kind }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="节选" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.truncated" type="warning" size="small">已节选</el-tag>
            <span v-else class="sh-muted">全文</span>
          </template>
        </el-table-column>
        <el-table-column label="生成时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click="openHistory(row.id)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <CostConfirmDialog
      v-model="confirmVisible"
      title="确认生成推广文案（计费）"
      description="将用 DeepSeek 生成小红书 / 微博 / 抖音三份文案，并保存到历史记录。"
      :cost="costNote"
      confirm-text="确认生成"
      :loading="generating"
      @confirm="generate"
    />
  </div>
</template>
