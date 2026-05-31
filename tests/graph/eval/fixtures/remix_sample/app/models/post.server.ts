export interface Post {
  id: number;
  title: string;
  authorId: number;
}

export function listPosts(): Post[] {
  return [{ id: 1, title: "Hello", authorId: 1 }];
}

export function getPostById(id: number): Post | null {
  return { id, title: "Hello", authorId: 1 };
}
