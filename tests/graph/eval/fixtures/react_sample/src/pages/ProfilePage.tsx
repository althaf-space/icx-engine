import React from "react";
import { useCurrentUser } from "../hooks/useCurrentUser";

export function ProfilePage() {
  const user = useCurrentUser();
  return (
    <section>
      <h1>Profile</h1>
      <p>ID: {user.id}</p>
      <p>Name: {user.name}</p>
    </section>
  );
}
