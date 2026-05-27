import { getUserByEmail } from "../models/user.server";
import { getSession } from "./session.server";

export async function authenticate(email: string, password: string) {
  const user = getUserByEmail(email);
  if (!user) return null;
  return user;
}

export async function requireUserId(request: Request): Promise<number> {
  const session = await getSession(request);
  return session.userId;
}
