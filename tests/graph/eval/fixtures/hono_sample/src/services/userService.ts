import { getDb } from "../db";

export interface User {
  id: number;
  name: string;
  email: string;
}

export function listUsers(): User[] {
  return getDb().users;
}

export function getUser(id: number): User | undefined {
  return getDb().users.find((u) => u.id === id);
}
