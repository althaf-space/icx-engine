import type { Actions } from "./$types";
import { login } from "$lib/stores/auth";

export const actions: Actions = {
  default: async ({ request }) => {
    const data = await request.formData();
    const userId = Number(data.get("userId"));
    await login(userId);
    return { success: true };
  },
};
