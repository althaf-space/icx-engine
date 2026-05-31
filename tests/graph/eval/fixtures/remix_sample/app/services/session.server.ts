export interface Session {
  userId: number;
}

export async function getSession(request: Request): Promise<Session> {
  return { userId: 1 };
}

export async function createSession(userId: number): Promise<string> {
  return `session-${userId}`;
}
