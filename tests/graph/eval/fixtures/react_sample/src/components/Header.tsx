import React from "react";
import { useCurrentUser } from "../hooks/useCurrentUser";

export function Header() {
  const user = useCurrentUser();
  return <header>Welcome, {user.name}</header>;
}
