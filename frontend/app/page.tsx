"use client";

import { useState } from "react";
import Homepage from "./homepage/homepage";
import AdminPanel from "./admin/admin";

export default function Page() {
  const [currentView, setCurrentView] = useState<"home" | "hub">("home");

  if (currentView === "hub") {
    return <AdminPanel onBack={() => setCurrentView("home")} />;
  }

  return (
    <Homepage
      onSignIn={() => setCurrentView("hub")}
      onAdmin={() => setCurrentView("hub")}
    />
  );
}
