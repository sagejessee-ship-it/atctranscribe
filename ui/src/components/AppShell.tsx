import { useState, type ReactNode } from "react";
import { NavLink } from "react-router";
import { Keyboard, UserRound } from "lucide-react";

import { useAnnotator } from "../lib/annotator";
import { Button, Dialog, IconButton } from "./ui";

export function PrimaryNav({ onHelp }: { onHelp: () => void }) {
  const [annotator, setAnnotator] = useAnnotator();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(annotator);
  return (
    <nav className="nav" aria-label="Primary">
      <span className="nav__brand">AeroChorus</span>
      <NavLink to="/review" className={({ isActive }) => (isActive ? "nav__link nav__link--active" : "nav__link")}>
        Review
      </NavLink>
      <NavLink
        to="/transcribe"
        className={({ isActive }) => (isActive ? "nav__link nav__link--active" : "nav__link")}
      >
        Transcribe
      </NavLink>
      <NavLink
        to="/training"
        className={({ isActive }) => (isActive ? "nav__link nav__link--active" : "nav__link")}
      >
        Training Sets
      </NavLink>
      <NavLink
        to="/adjudicate"
        className={({ isActive }) => (isActive ? "nav__link nav__link--active" : "nav__link")}
      >
        Adjudication
      </NavLink>
      <span className="nav__spacer" />
      <Button
        variant="ghost"
        size="sm"
        onClick={() => {
          setDraft(annotator);
          setEditing(true);
        }}
        title="Annotator recorded on every save"
      >
        <UserRound size={13} aria-hidden /> {annotator}
      </Button>
      <IconButton label="Keyboard shortcuts (?)" onClick={onHelp}>
        <Keyboard size={15} />
      </IconButton>
      <Dialog
        open={editing}
        onOpenChange={setEditing}
        title="Annotator"
        description="Recorded with every annotation version on this browser. Not authentication."
      >
        <form
          className="form"
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim()) setAnnotator(draft.trim());
            setEditing(false);
          }}
        >
          <label className="field">
            <span>Name or initials</span>
            <input className="input" value={draft} autoFocus onChange={(e) => setDraft(e.target.value)} />
          </label>
          <div className="dialog__actions">
            <Button type="submit" variant="primary">
              Save
            </Button>
          </div>
        </form>
      </Dialog>
    </nav>
  );
}

export function AppShell({ children, onHelp }: { children: ReactNode; onHelp: () => void }) {
  return (
    <div className="shell">
      <PrimaryNav onHelp={onHelp} />
      <main className="shell__main">{children}</main>
    </div>
  );
}
