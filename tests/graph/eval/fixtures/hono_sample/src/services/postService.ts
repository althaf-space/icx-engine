import { getDb } from "../db";

export interface Post {
  id: number;
  title: string;
  authorId: number;
}

export function listPosts(): Post[] {
  return getDb().posts;
}

export function createPost(title: string, authorId: number): Post {
  const db = getDb();
  const post = { id: db.posts.length + 1, title, authorId };
  db.posts.push(post);
  return post;
}
