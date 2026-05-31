import { defineStore } from "pinia";
import { ref } from "vue";
import { login as apiLogin } from "../services/api";

export const useUserStore = defineStore("user", () => {
  const currentUser = ref<{ id: number; name: string } | null>(null);

  async function login(email: string, password: string) {
    currentUser.value = await apiLogin(email, password);
  }

  function clearUser() {
    currentUser.value = null;
  }

  return { currentUser, login, clearUser };
});
