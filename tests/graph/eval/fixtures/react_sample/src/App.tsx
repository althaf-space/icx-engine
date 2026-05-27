import React from "react";
import { HomePage } from "./pages/HomePage";
import { ProfilePage } from "./pages/ProfilePage";
import { AppShell } from "./components/AppShell";

export default function App() {
  return (
    <AppShell>
      <HomePage />
      <ProfilePage />
    </AppShell>
  );
}
