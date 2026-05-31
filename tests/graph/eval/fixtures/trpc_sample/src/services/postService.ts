import { getDb } from "../db/prisma";

export function listPosts() {
  return getDb().post.findMany();
}

export function createPost(title: string, authorId: number) {
  return getDb().post.create({ title, authorId });
}
