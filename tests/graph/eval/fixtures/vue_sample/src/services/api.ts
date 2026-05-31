export async function login(email: string, password: string) {
  const res = await fetch("/api/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  return res.json();
}

export async function fetchAllPosts() {
  const res = await fetch("/api/posts");
  return res.json();
}
