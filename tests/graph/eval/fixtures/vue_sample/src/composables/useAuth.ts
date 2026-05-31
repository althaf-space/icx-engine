import { computed } from "vue";
import { useUserStore } from "../stores/user";

export function useAuth() {
  const store = useUserStore();
  const isLoggedIn = computed(() => store.currentUser !== null);

  function logout() {
    store.clearUser();
  }

  return { isLoggedIn, logout };
}
