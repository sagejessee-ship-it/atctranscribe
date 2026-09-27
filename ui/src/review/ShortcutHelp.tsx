import { Dialog, Kbd } from "../components/ui";

export const SHORTCUTS: [string[], string][] = [
  [["Space"], "Play / pause"],
  [["J", "↑"], "Previous segment"],
  [["K", "↓"], "Next segment"],
  [["R"], "Replay the selected span, or from the start"],
  [["L"], "Looping on/off (default on: selected span, else whole file)"],
  [["C"], "Focus the correction editor"],
  [["A"], "Use the selected hypothesis as correction starting text"],
  [["S"], "Mark silver (human text if typed, else model consensus)"],
  [["G"], "Mark human-verified gold (asks for confirmation)"],
  [["X"], "Reject for training"],
  [["Ctrl", "↵"], "Save the annotation (in the editor)"],
  [["Esc"], "Leave the editor"],
  [["/"], "Search transcripts"],
  [["?"], "This help"],
];

export function ShortcutHelp({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Keyboard shortcuts"
      description="Active when focus is not in a text field."
      width={420}
    >
      <table className="mini-table shortcuts">
        <tbody>
          {SHORTCUTS.map(([keys, action]) => (
            <tr key={action}>
              <td>
                {keys.map((k, i) => (
                  <span key={k}>
                    {i > 0 ? " " : ""}
                    <Kbd>{k}</Kbd>
                  </span>
                ))}
              </td>
              <td>{action}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Dialog>
  );
}
