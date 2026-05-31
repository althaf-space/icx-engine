import { writable } from "svelte/store";
import { fetchUser } from "../services/api";

export const currentUser = writable<{ id: number; name: string } | null>(null);

export async function login(userId: number) {
  const user = await fetchUser(userId);
  currentUser.set(user);
}
