<script setup lang="ts">
import { useAuth } from "../composables/useAuth";
import { useUserStore } from "../stores/user";

const { isLoggedIn } = useAuth();
const userStore = useUserStore();
</script>

<template>
  <div v-if="!isLoggedIn">
    <button @click="userStore.login('a@b.c', 'pass')">Login</button>
  </div>
</template>
