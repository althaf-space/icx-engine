interface Db {
  users: { id: number; name: string; email: string }[];
  posts: { id: number; title: string; authorId: number }[];
}

const db: Db = { users: [], posts: [] };

export function getDb(): Db {
  return db;
}
