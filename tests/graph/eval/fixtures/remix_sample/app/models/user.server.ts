export interface User {
  id: number;
  email: string;
  name: string;
}

export function getUserById(id: number): User | null {
  return { id, email: "test@test.com", name: "Test User" };
}

export function getUserByEmail(email: string): User | null {
  return { id: 1, email, name: "Test" };
}
