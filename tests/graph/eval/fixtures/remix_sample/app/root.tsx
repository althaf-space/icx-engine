import { Outlet } from "@remix-run/react";
import NavBar from "./components/NavBar";

export default function App() {
  return (
    <html>
      <body>
        <NavBar />
        <Outlet />
      </body>
    </html>
  );
}
