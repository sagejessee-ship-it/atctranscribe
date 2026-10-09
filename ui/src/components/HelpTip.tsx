import { Info } from "lucide-react";
import { Popover } from "radix-ui";
import type { ReactNode } from "react";

/**
 * A small (i) button that opens a short explanation. Click (or Enter/Space) opens it;
 * Esc or a click elsewhere closes it.
 */
export function HelpTip({
  label,
  children,
  wide = false,
}: {
  label: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button type="button" className="helptip" aria-label={`Help: ${label}`} onClick={(e) => e.stopPropagation()}>
          <Info size={13} aria-hidden />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className={wide ? "helptip__content helptip__content--wide" : "helptip__content"}
          side="bottom"
          align="start"
          sideOffset={4}
          collisionPadding={12}
        >
          <div className="helptip__title">{label}</div>
          {children}
          <Popover.Arrow className="helptip__arrow" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
