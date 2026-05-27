export interface User {
  id: number;
  email: string;
  name: string;
}

export interface Post {
  id: number;
  title: string;
  authorId: number;
}

export function getDb() {
  return {
    user: { findMany: () => [] as User[], findUnique: (id: number) => null as User | null },
    post: { findMany: () => [] as Post[], create: (data: Partial<Post>) => ({} as Post) },
  };
}
