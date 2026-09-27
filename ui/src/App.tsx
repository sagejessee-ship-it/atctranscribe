import { useState } from "react";
import { Navigate, Route, Routes } from "react-router";

import { AdjudicationPage } from "./adjudicate/AdjudicationPage";
import { AppShell } from "./components/AppShell";
import { ReviewPage } from "./review/ReviewPage";
import { ShortcutHelp } from "./review/ShortcutHelp";
import { TrainingPage } from "./training/TrainingPage";
import { TranscribePage } from "./transcribe/TranscribePage";

export function App() {
  const [help, setHelp] = useState(false);
  return (
    <AppShell onHelp={() => setHelp(true)}>
      <Routes>
        <Route path="/" element={<Navigate to="/review" replace />} />
        <Route path="/review" element={<ReviewPage onHelp={() => setHelp(true)} />} />
        <Route path="/transcribe" element={<TranscribePage />} />
        <Route path="/training" element={<TrainingPage />} />
        <Route path="/adjudicate" element={<AdjudicationPage />} />
        <Route path="*" element={<Navigate to="/review" replace />} />
      </Routes>
      <ShortcutHelp open={help} onOpenChange={setHelp} />
    </AppShell>
  );
}
