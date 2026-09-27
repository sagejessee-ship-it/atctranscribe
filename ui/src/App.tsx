import { useState } from "react";
import { Navigate, Route, Routes } from "react-router";

import { AppShell } from "./components/AppShell";
import { ReviewPage } from "./review/ReviewPage";
import { ShortcutHelp } from "./review/ShortcutHelp";
import { TrainingPage } from "./training/TrainingPage";

export function App() {
  const [help, setHelp] = useState(false);
  return (
    <AppShell onHelp={() => setHelp(true)}>
      <Routes>
        <Route path="/" element={<Navigate to="/review" replace />} />
        <Route path="/review" element={<ReviewPage onHelp={() => setHelp(true)} />} />
        <Route path="/training" element={<TrainingPage />} />
        <Route path="*" element={<Navigate to="/review" replace />} />
      </Routes>
      <ShortcutHelp open={help} onOpenChange={setHelp} />
    </AppShell>
  );
}
