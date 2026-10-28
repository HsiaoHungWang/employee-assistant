<script setup>
import { computed, nextTick, reactive, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { postJSON, streamPost } from '../api'
import ConfirmCard from './ConfirmCard.vue'

const props = defineProps({ session: Object })
const emit = defineEmits(['logout'])

const messages = ref([])
const input = ref('')
const busy = ref(false)
const listEl = ref(null)
const budget = ref(null)   // 本次對話累計用量（後端 TokenBudget）

const user = computed(() => props.session.user)
const suggestions = computed(() => [
  '我還剩幾天特休？',
  '幫我請下週五特休一天，家裡有事',
  '明天下午兩點到三點，有可以 6 個人開會的會議室嗎？',
  '出差回來報帳期限是幾天？',
  ...(user.value.role === 'manager' ? ['我的部屬最近有誰請假？', '幫我核准林志豪的事假'] : []),
])

const md = (text) => DOMPurify.sanitize(marked.parse(text || ''))

function scrollDown() {
  nextTick(() => listEl.value?.scrollTo({ top: listEl.value.scrollHeight, behavior: 'smooth' }))
}

// 依事件 type 更新畫面上的同一個泡泡
function handleEvent(bot, e) {
  if (e.type === 'delta') bot.text += e.text
  else if (e.type === 'tool') {
    const t = bot.tools.find((x) => x.name === e.name && x.status === 'running')
    if (t) t.status = e.status
    else bot.tools.push({ name: e.name, label: e.label, status: e.status })
  } else if (e.type === 'confirm') {
    bot.action = e.action
    bot.actionState = 'pending'
  } else if (e.type === 'notice') bot.notice = e.message
  else if (e.type === 'error') bot.error = e.message
  else if (e.type === 'done') {
    bot.usage = e.usage
    budget.value = e.budget
  }
  scrollDown()
}

async function run(url, body) {
  const bot = reactive({ role: 'assistant', text: '', tools: [], action: null, actionState: null,
                         notice: '', error: '', usage: null })
  messages.value.push(bot)
  busy.value = true
  try {
    await streamPost(url, body, props.session.token, (e) => handleEvent(bot, e))
  } catch (e) {
    bot.error = e.message
  } finally {
    busy.value = false
    scrollDown()
  }
}

async function send(text = input.value) {
  text = text.trim()
  if (!text || busy.value) return
  // 還沒確認就送新訊息：後端會把待確認的操作取消，畫面同步標成失效
  messages.value.forEach((m) => m.actionState === 'pending' && (m.actionState = 'expired'))
  messages.value.push({ role: 'user', text })
  input.value = ''
  scrollDown()
  await run('/api/chat', { message: text })
}

async function decide(msg, approved) {
  if (busy.value) return
  msg.actionState = approved ? 'approved' : 'rejected'
  await run('/api/confirm', { action_id: msg.action.id, approved })
}

async function reset() {
  await postJSON('/api/reset', {}, props.session.token)
  messages.value = []
  budget.value = null
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="me">
        <span class="avatar">{{ user.name.slice(0, 1) }}</span>
        <div>
          <strong>{{ user.name }}</strong>
          <small>{{ user.dept }}／{{ user.title }}</small>
        </div>
      </div>

      <h3>試試看</h3>
      <button v-for="s in suggestions" :key="s" class="chip" :disabled="busy" @click="send(s)">{{ s }}</button>

      <h3>可用工具（依角色）</h3>
      <ul class="tool-list">
        <li v-for="t in session.tools" :key="t.name">
          {{ t.label }} <span v-if="t.write" class="tag">需確認</span>
        </li>
      </ul>

      <template v-if="budget">
        <h3>本次對話用量</h3>
        <div class="meter"><span :style="{ width: Math.min(100, (budget.used / budget.max) * 100) + '%' }" /></div>
        <small>{{ budget.used.toLocaleString() }} / {{ budget.max.toLocaleString() }} tokens</small>
      </template>

      <div class="sidebar-actions">
        <button @click="reset">清除對話</button>
        <button @click="emit('logout')">登出</button>
      </div>
    </aside>

    <main class="chat">
      <div ref="listEl" class="messages">
        <p v-if="!messages.length" class="empty">嗨 {{ user.name }}，想查假勤、規章或預約會議室都可以問我。</p>
        <div v-for="(m, i) in messages" :key="i" class="msg" :class="m.role">
          <template v-if="m.role === 'user'">{{ m.text }}</template>
          <template v-else>
            <p v-if="m.notice" class="notice">🛡 {{ m.notice }}</p>
            <div v-if="m.tools.length" class="tools">
              <span v-for="(t, j) in m.tools" :key="j" class="tool" :class="t.status">
                {{ t.status === 'running' ? '⏳' : t.status === 'done' ? '✓' : '✕' }} {{ t.label }}
              </span>
            </div>
            <div v-if="m.text" class="md" v-html="md(m.text)" />
            <ConfirmCard v-if="m.action" :action="m.action" :state="m.actionState" @decide="decide(m, $event)" />
            <p v-if="m.error" class="error">{{ m.error }}</p>
            <p v-if="!m.text && !m.tools.length && !m.action && !m.error && !m.notice" class="muted typing">思考中…</p>
            <small v-if="m.usage && m.usage.prompt_tokens" class="usage">
              tokens：輸入 {{ m.usage.prompt_tokens }}／輸出 {{ m.usage.completion_tokens }}
            </small>
          </template>
        </div>
      </div>

      <form class="composer" @submit.prevent="send()">
        <input v-model="input" :disabled="busy" placeholder="輸入問題，例如：幫我請明天病假" maxlength="1000" />
        <button class="primary" :disabled="busy || !input.trim()">送出</button>
      </form>
    </main>
  </div>
</template>
