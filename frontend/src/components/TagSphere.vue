<script setup>
/**
 * 3D 旋转标签球：知识标签的可视化。
 *
 * 实现选择：把每个标签画成 **canvas 贴图的 Sprite**，而不是用 CSS2DRenderer。
 * 理由是两个都要做到时才体现出来——**文字要清晰**（canvas 按 devicePixelRatio 绘制）
 * 且**能被射线拾取**（Sprite 是真实场景对象，Raycaster 直接可用；CSS2D 的标签是 DOM，
 * 还得额外把坐标投影回屏幕再手写命中判断）。所以这条路少一层自己维护的数学。
 *
 * 交互：自动缓慢自转；鼠标拖动改变朝向；悬停高亮并显示说明；点击选中。
 * 卸载时释放全部 GPU 资源——Three.js 不会自己回收，切页面多了会明显泄漏显存。
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as THREE from 'three'

const props = defineProps({
  tags: { type: Array, default: () => [] },
  /** 球半径（相对容器尺寸的比例）。 */
  radius: { type: Number, default: 0.62 },
})

const emit = defineEmits(['select'])

const container = ref(null)
const hovered = ref(null)
const selected = ref(null)

// Three.js 对象不放进 reactive：深层代理会有明显性能损耗。
const scene = shallowRef(null)
const renderer = shallowRef(null)
const camera = shallowRef(null)
const group = shallowRef(null)
const sprites = shallowRef([])
const raycaster = shallowRef(null)
const pointer = shallowRef(new THREE.Vector2())
const frame = shallowRef(0)

//: 分类配色。分类是后端给的 kind，未知分类落到默认色。
const KIND_COLORS = {
  基础理论: '#409eff',
  编程语言: '#67c23a',
  系统网络: '#e6a23c',
  数据与AI: '#a855f7',
  工程实践: '#14b8a6',
  数学基础: '#f472b6',
  职业发展: '#f56c6c',
}
const DEFAULT_COLOR = '#909399'

function colorFor(kind) {
  return KIND_COLORS[kind] || DEFAULT_COLOR
}

/** 把标签文字画成一张贴图。按 devicePixelRatio 绘制，所以放大也不糊。 */
function makeLabelTexture(tag) {
  const ratio = Math.min(window.devicePixelRatio || 1, 2)
  const fontSize = 40
  const padding = 12
  const canvas = document.createElement('canvas')
  const context = canvas.getContext('2d')
  context.font = `600 ${fontSize}px "Microsoft YaHei", system-ui, sans-serif`
  const text = tag.name || ''
  const width = Math.ceil(context.measureText(text).width) + padding * 2
  const height = fontSize + padding * 2
  canvas.width = width * ratio
  canvas.height = height * ratio

  const ctx = canvas.getContext('2d')
  ctx.scale(ratio, ratio)
  ctx.font = `600 ${fontSize}px "Microsoft YaHei", system-ui, sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  // 圆角底板：让文字在任何背景上都读得清。
  const radius = height / 2
  ctx.fillStyle = 'rgba(17, 24, 39, 0.82)'
  ctx.beginPath()
  ctx.moveTo(radius, 0)
  ctx.lineTo(width - radius, 0)
  ctx.arc(width - radius, radius, radius, -Math.PI / 2, Math.PI / 2)
  ctx.lineTo(radius, height)
  ctx.arc(radius, radius, radius, Math.PI / 2, -Math.PI / 2)
  ctx.closePath()
  ctx.fill()
  ctx.strokeStyle = colorFor(tag.kind)
  ctx.lineWidth = 3
  ctx.stroke()
  ctx.fillStyle = '#ffffff'
  ctx.fillText(text, width / 2, height / 2 + 2)

  const texture = new THREE.CanvasTexture(canvas)
  texture.minFilter = THREE.LinearFilter
  texture.needsUpdate = true
  // 同时返回宽高比：世界坐标里的尺寸按球半径算，不按 canvas 像素算。
  return { texture, aspect: width / height }
}

/** 斐波那契球分布：点均匀且不会在两极堆积（直接用经纬度会堆）。 */
function fibonacciSphere(count, radius) {
  const points = []
  const golden = Math.PI * (3 - Math.sqrt(5))
  for (let index = 0; index < count; index += 1) {
    const y = 1 - (index / Math.max(1, count - 1)) * 2
    const r = Math.sqrt(Math.max(0, 1 - y * y))
    const theta = golden * index
    points.push(new THREE.Vector3(Math.cos(theta) * r * radius, y * radius, Math.sin(theta) * r * radius))
  }
  return points
}

function buildSprites() {
  const active = group.value
  if (!active) return
  // 重建前清掉旧的，避免切换词表时叠影。
  for (const sprite of sprites.value) {
    active.remove(sprite)
    sprite.material.map?.dispose()
    sprite.material.dispose()
  }
  sprites.value = []

  const tags = props.tags
  if (!tags.length) return

  const node = container.value
  const width = node?.clientWidth || 900
  const height = node?.clientHeight || 520
  // **球体占满容器的短边**，而不是按宽度的一半再乘系数——之前那样球偏小。
  const sphereRadius = Math.min(width, height) * 0.42
  const points = fibonacciSphere(tags.length, sphereRadius)

  // 标签的世界高度按**球半径**取，与 canvas 像素无关。之前用「canvas 像素 × 容器宽度/620」，
  // 容器一大标签就跟着暴涨（运营方反馈"星球太小、字体太大"）。
  // 0.115 让短标签约占球半径的 1/9，长标签靠 aspect 撑开宽度但仍然一行放得下。
  const labelHeight = sphereRadius * 0.115

  tags.forEach((tag, index) => {
    const { texture, aspect } = makeLabelTexture(tag)
    const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false })
    const sprite = new THREE.Sprite(material)
    const scale = labelHeight * (1 + Math.min(0.35, (Number(tag.weight || 1) - 1) * 0.12))
    sprite.scale.set(scale * aspect, scale, 1)
    sprite.position.copy(points[index])
    sprite.userData = {
      tag,
      baseX: scale * aspect,
      baseY: scale,
    }
    active.add(sprite)
    sprites.value.push(sprite)
  })

  // 相机距离跟球半径走，球才会稳定地占满视野。
  if (camera.value) {
    camera.value.position.z = sphereRadius * 2.7
    camera.value.updateProjectionMatrix()
  }
}

function resize() {
  const node = container.value
  const active = renderer.value
  const activeCamera = camera.value
  if (!node || !active || !activeCamera) return
  const width = node.clientWidth || 600
  const height = node.clientHeight || 520
  active.setSize(width, height, false)
  activeCamera.aspect = width / height
  activeCamera.updateProjectionMatrix()
  buildSprites()
}

function onPointerMove(event) {
  const node = container.value
  if (!node) return
  const rect = node.getBoundingClientRect()
  pointer.value.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
  pointer.value.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
  if (dragging) {
    const deltaX = event.clientX - lastX
    const deltaY = event.clientY - lastY
    lastX = event.clientX
    lastY = event.clientY
    velocityX = deltaX * 0.005
    velocityY = deltaY * 0.005
  }
}

let dragging = false
let lastX = 0
let lastY = 0
let velocityX = 0.0022
let velocityY = 0

function onPointerDown(event) {
  dragging = true
  lastX = event.clientX
  lastY = event.clientY
  container.value?.setPointerCapture?.(event.pointerId)
}

function onPointerUp(event) {
  dragging = false
  container.value?.releasePointerCapture?.(event.pointerId)
}

function onClick() {
  const tag = hovered.value
  if (!tag) return
  selected.value = tag.name
  emit('select', tag)
}

function animate() {
  frame.value = requestAnimationFrame(animate)
  const active = renderer.value
  const activeCamera = camera.value
  const activeGroup = group.value
  if (!active || !activeCamera || !activeGroup) return

  if (!dragging) {
    // 惯性：拖动后继续转一会儿，再回到基础自转速度。
    velocityX += (0.0022 - velocityX) * 0.02
    velocityY *= 0.94
  }
  activeGroup.rotation.y += velocityX
  activeGroup.rotation.x += velocityY

  // 拾取：只对当前指针位置做，成本可忽略。
  raycaster.value.setFromCamera(pointer.value, activeCamera)
  const hits = raycaster.value.intersectObjects(sprites.value, false)
  const next = hits.length ? hits[0].object.userData.tag : null
  hovered.value = next
  for (const sprite of sprites.value) {
    const isHovered = next && sprite.userData.tag === next
    const factor = isHovered ? 1.2 : 1
    const targetX = sprite.userData.baseX * factor
    const targetY = sprite.userData.baseY * factor
    if (Math.abs(sprite.scale.x - targetX) > 0.0005) {
      sprite.scale.set(targetX, targetY, 1)
    }
    // 未悬停时压暗而非透明，保持球体可读。
    sprite.material.opacity = next && !isHovered ? 0.5 : 1
  }

  active.render(scene.value, activeCamera)
}

function dispose() {
  cancelAnimationFrame(frame.value)
  for (const sprite of sprites.value) {
    sprite.material.map?.dispose()
    sprite.material.dispose()
  }
  sprites.value = []
  renderer.value?.dispose()
  renderer.value?.domElement?.remove()
  renderer.value = null
  scene.value = null
  camera.value = null
  group.value = null
}

onMounted(() => {
  const node = container.value
  if (!node) return
  const width = node.clientWidth || 600
  const height = node.clientHeight || 520

  scene.value = new THREE.Scene()
  camera.value = new THREE.PerspectiveCamera(50, width / height, 1, 8000)
  camera.value.position.z = Math.min(width, height) * 1.15

  renderer.value = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.value.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.value.setSize(width, height, false)
  renderer.value.domElement.style.width = '100%'
  renderer.value.domElement.style.height = '100%'
  renderer.value.domElement.style.display = 'block'
  node.appendChild(renderer.value.domElement)

  group.value = new THREE.Group()
  scene.value.add(group.value)
  raycaster.value = new THREE.Raycaster()

  buildSprites()
  animate()
  window.addEventListener('resize', resize)
})

watch(() => props.tags, buildSprites, { deep: false })

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  dispose()
})

const hoveredBlurb = computed(() => hovered.value?.blurb || '')
</script>

<template>
  <div class="sh-sphere-wrap">
    <div
      ref="container"
      class="sh-sphere"
      @pointermove="onPointerMove"
      @pointerdown="onPointerDown"
      @pointerup="onPointerUp"
      @pointerleave="onPointerUp"
      @click="onClick"
    />
    <div class="sh-sphere-hint">
      <template v-if="hovered">
        <strong>{{ hovered.name }}</strong>
        <span class="sh-muted">
          · {{ hovered.kind || '未分类' }} · {{ hovered.difficulty || '未标注' }}
          <template v-if="hovered.article_count"> · 已生成 {{ hovered.article_count }} 篇</template>
        </span>
        <div v-if="hoveredBlurb" class="sh-muted">{{ hoveredBlurb }}</div>
        <div class="sh-muted">点击即生成这个方向的文案</div>
      </template>
      <span v-else class="sh-muted">
        拖动旋转 · 滚轮无效 · 悬停查看说明 · <strong>点击标签生成文案</strong>
      </span>
    </div>
  </div>
</template>

<style scoped>
.sh-sphere-wrap {
  position: relative;
  width: 100%;
}
.sh-sphere {
  width: 100%;
  height: 520px;
  cursor: grab;
  /* 深色底让彩色标签更清楚，也和 canvas 上画的深色底板一致。 */
  background: radial-gradient(circle at 50% 45%, #1f2937 0%, #0b1120 70%);
  border-radius: 10px;
}
.sh-sphere:active {
  cursor: grabbing;
}
.sh-sphere-hint {
  position: absolute;
  left: 12px;
  bottom: 10px;
  right: 12px;
  color: #e5e7eb;
  font-size: 12px;
  line-height: 1.6;
  pointer-events: none;
}
.sh-sphere-hint .sh-muted {
  color: #9ca3af;
}
</style>
