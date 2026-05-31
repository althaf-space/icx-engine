import { getDb } from "../db/prisma";

export function listUsers() {
  return getDb().user.findMany();
}

export function getUser(id: number) {
  return getDb().user.findUnique(id);
}
