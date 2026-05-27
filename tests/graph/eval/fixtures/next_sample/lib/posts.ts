export interface Post {
  id: number;
  title: string;
  body: string;
}

export async function listPosts(): Promise<Post[]> {
  return [{ id: 1, title: "Hello", body: "World" }];
}
