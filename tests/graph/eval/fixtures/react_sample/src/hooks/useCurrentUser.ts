import { useState, useEffect } from "react";

export interface CurrentUser {
  id: number;
  name: string;
}

export function useCurrentUser(): CurrentUser {
  const [user, setUser] = useState<CurrentUser>({ id: 0, name: "anon" });
  useEffect(() => {
    setUser({ id: 1, name: "alice" });
  }, []);
  return user;
}
