import type { ActionFunctionArgs } from "@remix-run/node";
import { redirect } from "@remix-run/node";
import { authenticate } from "../services/auth.server";
import { createSession } from "../services/session.server";

export async function action({ request }: ActionFunctionArgs) {
  const form = await request.formData();
  const email = form.get("email") as string;
  const password = form.get("password") as string;
  const user = await authenticate(email, password);
  if (!user) return { error: "Invalid credentials" };
  await createSession(user.id);
  return redirect("/");
}

export default function Login() {
  return (
    <form method="post">
      <input name="email" type="email" />
      <input name="password" type="password" />
      <button type="submit">Login</button>
    </form>
  );
}
