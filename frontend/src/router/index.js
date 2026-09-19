import { createRouter, createWebHistory } from 'vue-router'

// Lazy-loaded so the first paint does not wait for Element Plus tables.
const routes = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { title: '总览' },
  },
  {
    path: '/hot',
    name: 'hot',
    component: () => import('@/views/HotListView.vue'),
    meta: { title: '热点列表' },
  },
  {
    path: '/topics',
    name: 'topics',
    component: () => import('@/views/TopicsView.vue'),
    meta: { title: '话题聚合' },
  },
  {
    path: '/topics/:id',
    name: 'topic-detail',
    component: () => import('@/views/TopicDetailView.vue'),
    meta: { title: '话题详情' },
  },
  {
    path: '/rewrites',
    name: 'rewrites',
    component: () => import('@/views/RewriteListView.vue'),
    meta: { title: '二创审核' },
  },
  {
    path: '/rewrites/:id',
    name: 'rewrite-detail',
    component: () => import('@/views/RewriteDetailView.vue'),
    meta: { title: '二创详情' },
  },
  {
    path: '/promo',
    name: 'promo',
    component: () => import('@/views/PromoView.vue'),
    meta: { title: '项目推广' },
  },
  {
    path: '/tasks',
    name: 'tasks',
    component: () => import('@/views/TasksView.vue'),
    meta: { title: '任务记录' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
    meta: { title: '知识科普' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = to.meta?.title
  document.title = title ? `${title} · SocialHot AI` : 'SocialHot AI 管理后台'
})

export default router
