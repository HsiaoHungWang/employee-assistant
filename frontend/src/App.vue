<script setup>
import { ref } from 'vue'
import LoginView from './components/LoginView.vue'
import ChatView from './components/ChatView.vue'

// session = { token, user, tools }：token 由後端核發，之後每次請求都帶著它
const session = ref(null)
</script>

<template>
  <LoginView v-if="!session" @login="session = $event" />
  <ChatView v-else :session="session" @logout="session = null" />
</template>
