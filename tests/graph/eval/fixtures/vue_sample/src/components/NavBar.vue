<script setup lang="ts">
import { useAuth } from "../composables/useAuth";

const { isLoggedIn, logout } = useAuth();
</script>

<template>
  <nav>
    <span v-if="isLoggedIn"><button @click="logout">Logout</button></span>
  </nav>
</template>
