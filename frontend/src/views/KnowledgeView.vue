<script setup>
/**
 * 知识科普 + 标签球（Phase 13）。
 *
 * 面向大学生计算机大类：点球体上的标签 → 生成一篇科普文章 + 三平台文案 + 配图。
 *
 * **页面上的每个付费动作都先确认**：
 * - 打开页面、切换文章、看列表 —— 免费（标签词表存库，复用不调模型）
 * - 「刷新标签」—— 1 次 DeepSeek 调用
 * - 点标签生成 —— 1-2 次 DeepSeek + 默认 1 次搜图
 * - 「生成配图」单点 —— 1 次搜图（$0.0078，一次多张；比生图便宜 4 倍）
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import CostConfirmDialog from '@/components/CostConfirmDialog.vue'
import TagSphere from '@/components/TagSphere.vue'
import { api } from '@/api/client'
import { copyText, formatDateTime } from '@/api/format'

const tags = ref([])
const tagsLoading = ref(false)
const tagsGenerated = ref(false)
const tagWarnings = ref([])
const selectedTag = ref(null)

const article = ref(null)
const articles = ref([])
const generateDialogVisible = ref(false)
const pendingTag = ref(null)
const extra = ref('')
const withPlatforms = ref(true)
const withImages = ref(true)
const imageCount = ref(6)

// --- 生成进度：文章要 30-60 秒，只转圈等于没信息 -----------------------------
// 每一步都有 started/done/failed 三个状态，来自后端的 NDJSON 流。**不是假进度条**：
// 界面只显示后端真的报过的事件。
const progress = ref([])
const generating = ref(false)
const elapsedSeconds = ref(0)
let elapsedTimer = null

const STEP_ORDER = ['article', 'platforms', 'images', 'save']

function resetProgress() {
  progress.value = []
  elapsedSeconds.value = 0
}

function onProgressEvent(event) {
  if (event.event === 'ping') return
  if (event.event !== 'step') return
  const index = progress.value.findIndex((item) => item.step === event.step)
  const entry = {
    step: event.step,
    label: event.label || event.step,
    status: event.status,
    message: '',
  }
  if (event.status === 'done') {
    if (event.sections) entry.message = `${event.sections} 节`
    else if (event.platforms?.length) entry.message = event.platforms.length + ' 个平台'
    else if (event.downloaded !== undefined) entry.message = `${event.downloaded} 张图`
    else if (event.article_id) entry.message = `#${event.article_id}`
  } else if (event.status === 'started' && event.note) {
    entry.message = event.note
  } else if (event.status === 'failed' && event.error) {
    entry.message = String(event.error).slice(0, 60)
  }
  if (index >= 0) progress.value[index] = entry
  else progress.value.push(entry)
}

const progressDone = computed(() => progress.value.filter((item) => item.status === 'done').length)
const progressFailed = computed(() => progress.value.some((item) => item.status === 'failed'))
const currentStep = computed(() => {
  const running = progress.value.find((item) => item.status === 'started')
  if (running) return running.label
  const last = progress.value[progress.value.length - 1]
  return last ? last.label : '准备中'
})

const refreshDialogVisible = ref(false)
const refreshing = ref(false)
const refreshExtra = ref('')

const activePlatform = ref('xiaohongshu')

const KIND_ORDER = ['基础理论', '编程语言', '系统网络', '数据与AI', '工程实践', '数学基础', '职业发展']
const groupedTags = computed(() => {
  const groups = {}
  for (const tag of tags.value) {
    const key = tag.kind || '未分类'
    groups[key] = groups[key] || []
    groups[key].push(tag)
  }
  return Object.entries(groups).sort(
    (a, b) => KIND_ORDER.indexOf(a[0]) - KIND_ORDER.indexOf(b[0]),
  )
})

async function loadTags() {
  tagsLoading.value = true
  try {
    const response = await api.knowledgeTags()
    tags.value = response.tags ?? []
    tagsGenerated.value = Boolean(response.generated)
    tagWarnings.value = response.warnings ?? []
    if (tagsGenerated.value) {
      // 说明这次调用生成了词表——如实告诉用户，因为那是一次付费调用。
      ElMessage.info(
        `词表为空，已生成 ${response.total} 个标签（消耗一次 DeepSeek 调用）；之后打开页面不再花钱`,
      )
    }
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取标签失败')
  } finally {
    tagsLoading.value = false
  }
}

async function loadArticles() {
  try {
    const response = await api.knowledgeArticles({ limit: 20 })
    articles.value = response.items ?? []
  } catch {
    articles.value = []
  }
}

function onSelectTag(tag) {
  pendingTag.value = tag
  generateDialogVisible.value = true
}

async function confirmGenerate() {
  const tag = pendingTag.value
  if (!tag) return
  generating.value = true
  resetProgress()
  // 计时器让用户看到"还在动"，比一个静止的转圈好判断。
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value += 1
  }, 1000)
  try {
    let failure = ''
    await api.createKnowledgeArticleStream(
      {
        topic: tag.name,
        tag_id: tag.id,
        extra: extra.value,
        with_platforms: withPlatforms.value,
        with_images: withImages.value,
        image_count: imageCount.value,
      },
      (event) => {
        if (event.event === 'result') {
          article.value = event.article
          selectedTag.value = tag.name
          activePlatform.value = Object.keys(event.article.platforms || {})[0] || 'xiaohongshu'
          ElMessage.success(
            `已生成《${event.article.title}》，花费约 ¥${event.estimated_cny}`,
          )
        } else if (event.event === 'error') {
          failure = event.error || '生成失败'
        } else {
          onProgressEvent(event)
        }
      },
    )
    if (failure) ElMessage.error(failure)
    await Promise.all([loadArticles(), loadTags()])
  } catch (error) {
    ElMessage.error(error?.message || '生成失败')
  } finally {
    clearInterval(elapsedTimer)
    generating.value = false
  }
}

async function openArticle(row) {
  try {
    const response = await api.knowledgeArticle(row.id)
    article.value = response.article
    activePlatform.value = Object.keys(response.article.platforms || {})[0] || 'xiaohongshu'
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取失败')
  }
}

async function confirmRefresh() {
  refreshing.value = true
  try {
    const response = await api.refreshKnowledgeTags(refreshExtra.value)
    tags.value = response.tags ?? []
    tagWarnings.value = response.warnings ?? []
    ElMessage.success(
      `已刷新：新增 ${response.created} 个、更新 ${response.updated} 个，共 ${response.total} 个（约 ¥${response.estimated_cny}）`,
    )
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '刷新失败')
  } finally {
    refreshing.value = false
  }
}

/** 复制某一平台的成品文案。 */
const platformDraft = computed(() => {
  const platforms = article.value?.platforms ?? {}
  const node = platforms[activePlatform.value]
  if (!node) return ''
  if (activePlatform.value === 'douyin') {
    return [
      node.hook ? `【开场钩子】${node.hook}` : '',
      node.script,
      (node.scenes || []).length
        ? `【分镜建议】\n${node.scenes.map((scene, index) => `${index + 1}. ${scene}`).join('\n')}`
        : '',
      node.subtitles ? `【字幕】${node.subtitles}` : '',
      node.cta ? `【结尾引导】${node.cta}` : '',
    ]
      .filter(Boolean)
      .join('\n\n')
  }
  const hashtags = (node.hashtags || [])
    .map((tag) => (tag.startsWith('#') ? tag : `#${tag}`))
    .join(' ')
  return [node.title, node.content, hashtags].filter(Boolean).join('\n\n')
})

async function copyDraft() {
  const text = platformDraft.value
  if (!text) {
    ElMessage.warning('该平台暂无文案')
    return
  }
  if (await copyText(text)) ElMessage.success('已复制')
  else ElMessage.warning('浏览器拒绝了剪贴板访问，请手动选中复制')
}

const PLATFORM_LABELS = { xiaohongshu: '小红书', weibo: '微博', douyin: '抖音' }

onMounted(() => {
  loadTags()
  loadArticles()
})
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">知识科普</h2>
        <p class="sh-page-subtitle">
          面向大学计算机大类：点标签即生成科普文章 + 小红书/微博/抖音文案 + 配图
        </p>
      </div>
      <div>
        <el-button :loading="tagsLoading" @click="loadTags">刷新列表</el-button>
        <el-button type="primary" @click="refreshDialogVisible = true">
          <el-icon><Refresh /></el-icon>&nbsp;刷新标签
        </el-button>
      </div>
    </div>

    <el-alert
      v-for="warning in tagWarnings"
      :key="warning"
      type="warning"
      :closable="false"
      show-icon
      :title="warning"
      style="margin-bottom: 10px"
    />

    <el-card shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <span>知识标签球</span>
          <el-tag size="small" effect="plain">{{ tags.length }} 个标签</el-tag>
          <div class="sh-spacer" />
          <span class="sh-muted">打开页面免费（词表存库复用）· 点击标签才生成</span>
        </div>
      </template>

      <el-empty v-if="!tags.length && !tagsLoading" description="还没有标签，点右上角「刷新标签」生成" />
      <TagSphere v-else :tags="tags" @select="onSelectTag" />
    </el-card>

    <el-card shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <span>标签分类</span>
          <div class="sh-spacer" />
          <span class="sh-muted">也可以直接从分类里点</span>
        </div>
      </template>
      <div v-for="[kind, items] in groupedTags" :key="kind" style="margin-bottom: 10px">
        <div class="sh-muted" style="margin-bottom: 4px">{{ kind }}（{{ items.length }}）</div>
        <el-tag
          v-for="tag in items"
          :key="tag.id"
          class="sh-knowledge-tag"
          :type="selectedTag === tag.name ? 'primary' : 'info'"
          :effect="selectedTag === tag.name ? 'dark' : 'plain'"
          size="small"
          style="margin: 0 6px 6px 0; cursor: pointer"
          @click="onSelectTag(tag)"
        >
          {{ tag.name }}
          <span v-if="tag.article_count" class="sh-muted">·{{ tag.article_count }}</span>
        </el-tag>
      </div>
    </el-card>

    <!-- 生成进度：每一步都由后端真实事件驱动，不是假进度条 -->
    <el-card v-if="generating || progress.length" shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <span>生成进度</span>
          <el-tag v-if="generating" type="primary" size="small" effect="plain">
            正在：{{ currentStep }} · 已用 {{ elapsedSeconds }} 秒
          </el-tag>
          <el-tag v-else-if="progressFailed" type="warning" size="small">已完成（有步骤失败）</el-tag>
          <el-tag v-else type="success" size="small">已完成</el-tag>
          <div class="sh-spacer" />
          <span class="sh-muted">
            共 {{ STEP_ORDER.length }} 步，已完成 {{ progressDone }} 步 · 通常需要 30-60 秒
          </span>
        </div>
      </template>
      <div v-for="item in progress" :key="item.step" style="display: flex; gap: 8px; align-items: baseline">
        <el-icon v-if="item.status === 'done'" color="#67c23a"><CircleCheck /></el-icon>
        <el-icon v-else-if="item.status === 'failed'" color="#e6a23c"><WarningFilled /></el-icon>
        <el-icon v-else class="is-loading" color="#409eff"><Loading /></el-icon>
        <span :style="{ color: item.status === 'failed' ? '#e6a23c' : undefined }">
          {{ item.label }}
          <span v-if="item.message" class="sh-muted">—— {{ item.message }}</span>
        </span>
      </div>
      <div v-if="generating" class="sh-muted" style="margin-top: 8px">
        每一步都是真实调用（DeepSeek / 搜图），失败的那一步不会影响其他步骤。
      </div>
    </el-card>

    <!-- 生成的文章 -->
    <el-card v-if="article" shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <span>{{ article.title }}</span>
          <el-tag size="small" effect="plain">{{ article.difficulty || '未标注' }}</el-tag>
          <el-tag v-if="article.audience" size="small" type="info" effect="plain">
            {{ article.audience }}
          </el-tag>
          <div class="sh-spacer" />
          <span class="sh-muted">
            {{ article.prompt_tokens + article.completion_tokens }} tokens · 约 ¥{{ article.estimated_cny }}
          </span>
        </div>
      </template>

      <p v-if="article.hook" class="sh-draft">{{ article.hook }}</p>

      <div v-for="(section, index) in article.sections" :key="index" style="margin-top: 16px">
        <h4 style="margin: 0 0 6px">{{ index + 1 }}. {{ section.heading }}</h4>
        <p style="margin: 0; line-height: 1.8; white-space: pre-wrap">{{ section.body }}</p>
        <ul v-if="section.key_points?.length" style="margin: 6px 0 0; padding-left: 20px">
          <li v-for="point in section.key_points" :key="point" class="sh-muted">{{ point }}</li>
        </ul>
      </div>

      <template v-if="article.glossary?.length">
        <el-divider content-position="left">术语表</el-divider>
        <div v-for="entry in article.glossary" :key="entry.term" style="margin-bottom: 4px">
          <strong>{{ entry.term }}</strong>
          <span class="sh-muted">：{{ entry.explanation }}</span>
        </div>
      </template>

      <template v-if="article.takeaways?.length">
        <el-divider content-position="left">读完应该掌握</el-divider>
        <ul style="margin: 0; padding-left: 20px">
          <li v-for="item in article.takeaways" :key="item">{{ item }}</li>
        </ul>
      </template>

      <template v-if="article.further_reading?.length">
        <el-divider content-position="left">延伸</el-divider>
        <div v-for="entry in article.further_reading" :key="entry.title" style="margin-bottom: 4px">
          · {{ entry.title }}
          <span v-if="entry.note" class="sh-muted">—— {{ entry.note }}</span>
        </div>
      </template>

      <template v-if="article.tags?.length">
        <el-divider content-position="left">知识标签</el-divider>
        <el-tag
          v-for="tag in article.tags"
          :key="tag"
          size="small"
          effect="plain"
          style="margin: 0 6px 6px 0"
        >
          {{ tag }}
        </el-tag>
      </template>

      <template v-if="article.images?.length">
        <el-divider content-position="left">
          配图（{{ article.images.length }} 张，来自搜图）
        </el-divider>
        <div style="display: flex; gap: 8px; flex-wrap: wrap">
          <el-image
            v-for="(image, index) in article.images"
            :key="image.local_path"
            :src="`/${image.local_path}`"
            :preview-src-list="article.images.map((item) => `/${item.local_path}`)"
            :initial-index="index"
            preview-teleported
            fit="cover"
            style="width: 110px; height: 110px; border-radius: 6px"
          />
        </div>
        <div class="sh-muted" style="margin-top: 6px">
          图片搜自小红书公开笔记，已下载到本地素材库；下面保留作者与原文链接用于署名。
        </div>
        <div v-for="image in article.images" :key="`${image.local_path}-credit`" class="sh-muted">
          · {{ image.title?.slice(0, 30) || '（无标题）' }} ——
          {{ image.author || '未知作者' }}
          <el-link v-if="image.link" :href="image.link" target="_blank" type="primary">
            原文
          </el-link>
        </div>
      </template>

      <template v-if="Object.keys(article.platforms || {}).length">
        <el-divider content-position="left">三平台文案</el-divider>
        <el-tabs v-model="activePlatform">
          <el-tab-pane
            v-for="(node, platform) in article.platforms"
            :key="platform"
            :label="PLATFORM_LABELS[platform] || platform"
            :name="platform"
          />
        </el-tabs>
        <pre class="sh-draft" style="white-space: pre-wrap">{{ platformDraft }}</pre>
        <el-button size="small" type="primary" @click="copyDraft">复制{{ PLATFORM_LABELS[activePlatform] }}文案</el-button>
        <span class="sh-muted" style="margin-left: 8px">发布请自行到平台操作，系统不会代为发布</span>
      </template>
      <el-alert
        v-else
        type="info"
        :closable="false"
        show-icon
        title="这次没有生成三平台文案"
        description="可能在生成时失败了（文章本身已保存），可以重新点一次标签生成。"
        style="margin-top: 12px"
      />
    </el-card>

    <!-- 历史 -->
    <el-card v-if="articles.length" shadow="never" class="sh-card">
      <template #header>已生成的文章（{{ articles.length }}）</template>
      <el-table :data="articles" size="small" @row-click="openArticle">
        <el-table-column label="标题" min-width="220">
          <template #default="{ row }">
            <div>{{ row.title }}</div>
            <div class="sh-muted">{{ row.topic }} · {{ formatDateTime(row.created_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="难度" width="80" prop="difficulty" />
        <el-table-column label="配图" width="70">
          <template #default="{ row }">{{ row.image_count }} 张</template>
        </el-table-column>
        <el-table-column label="平台" width="150">
          <template #default="{ row }">
            <el-tag v-for="p in row.platforms" :key="p" size="small" effect="plain" style="margin-right: 4px">
              {{ PLATFORM_LABELS[p] || p }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="花费" width="90">
          <template #default="{ row }">¥{{ row.estimated_cny }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openArticle(row)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <CostConfirmDialog
      v-model="generateDialogVisible"
      title="生成这个方向的科普文案（计费）"
      :description="`将为「${pendingTag?.name ?? ''}」生成：一篇科普文章 + 三平台文案${withImages ? ' + 配图（搜图）' : ''}。`"
      cost="文章 1 次 DeepSeek、三平台文案 1 次 DeepSeek；配图默认用搜图（1 次 TikHub 调用约 $0.0078，一次拿多张，比生图便宜 4 倍）。合计约 ¥0.03-0.06。"
      confirm-text="确认生成"
      :loading="generating"
      @confirm="confirmGenerate"
    >
      <el-form label-width="90px" style="margin-top: 10px">
        <el-form-item label="额外要求">
          <el-input v-model="extra" placeholder="可选：如「多给代码示例」「多讲和面试的关系」" />
        </el-form-item>
        <el-form-item label="配图">
          <el-checkbox v-model="withImages">搜图并下载配图</el-checkbox>
          <el-input-number v-model="imageCount" :min="1" :max="20" size="small" style="margin-left: 10px" />
          <span class="sh-muted">张</span>
        </el-form-item>
        <el-form-item label="三平台">
          <el-checkbox v-model="withPlatforms">同时生成小红书/微博/抖音文案</el-checkbox>
        </el-form-item>
      </el-form>
    </CostConfirmDialog>

    <CostConfirmDialog
      v-model="refreshDialogVisible"
      title="刷新标签词表（计费）"
      description="重新生成 30-50 个知识标签，覆盖大类、细分技术、语言工具、数学基础与职业发展。已有人工调整过的标签会保留分类与权重。"
      cost="消耗 1 次 DeepSeek 调用，约 ¥0.01-0.03。平时打开页面是免费的——词表存库复用。"
      confirm-text="确认刷新"
      :loading="refreshing"
      @confirm="confirmRefresh"
    >
      <el-form label-width="90px" style="margin-top: 10px">
        <el-form-item label="额外要求">
          <el-input v-model="refreshExtra" placeholder="可选：如「多来点 AI 方向的」" />
        </el-form-item>
      </el-form>
    </CostConfirmDialog>
  </div>
</template>
