export async function fetchPosts() {
  const res = await fetch("/api/posts");
  return res.json();
}

export async function fetchUser(id: number) {
  const res = await fetch(`/api/users/${id}`);
  return res.json();
}
