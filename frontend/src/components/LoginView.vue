<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'

const emit = defineEmits(['login'])
const users = ref([])
const error = ref('')

onMounted(async () => {
  try {
    users.value = await getJSON('/api/users')
  } catch {
    error.value = '連不到後端，請確認 FastAPI 已在 8000 埠啟動'
  }
})

async function login(id) {
  try {
    emit('login', await postJSON('/api/login', { employee_id: id }))
  } catch (e) {
    error.value = e.message
  }
}
</script>

<template>
  <main class="login">
    <div class="login-card">
      <h1>員工內部助理</h1>
      <p class="muted">示範用登入：選一位員工（正式系統換成公司 SSO）</p>
      <button v-for="u in users" :key="u.id" class="user-btn" @click="login(u.id)">
        <span class="avatar">{{ u.name.slice(0, 1) }}</span>
        <span>
          <strong>{{ u.name }}</strong>
          <small>{{ u.dept }}／{{ u.title }}</small>
        </span>
        <span v-if="u.role === 'manager'" class="badge">主管</span>
      </button>
      <p v-if="error" class="error">{{ error }}</p>
    </div>
  </main>
</template>
