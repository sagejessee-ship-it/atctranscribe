// shadcn/ui-style primitives: thin, locally owned wrappers over Radix with our tokens.
import clsx from "clsx";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Dialog as RDialog, DropdownMenu } from "radix-ui";
import { X } from "lucide-react";

type Variant = "default" | "primary" | "ghost" | "danger";

export const Button = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }
>(function Button({ variant = "default", size = "md", className, type = "button", ...rest }, ref) {
  return (
    <button
      ref={ref}
      type={type}
      className={clsx("btn", `btn--${variant}`, size === "sm" && "btn--sm", className)}
      {...rest}
    />
  );
});

/** Icon-only button: `label` is required and becomes the accessible name and tooltip. */
export const IconButton = forwardRef<
  HTMLButtonElement,
  ButtonHTMLAttributes<HTMLButtonElement> & { label: string; variant?: Variant }
>(function IconButton({ label, variant = "ghost", className, type = "button", ...rest }, ref) {
  return (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      title={label}
      className={clsx("btn", "btn--icon", `btn--${variant}`, className)}
      {...rest}
    />
  );
});

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  width = 440,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  width?: number;
}) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="dialog-overlay" />
        <RDialog.Content className="dialog" style={{ width }}>
          <div className="dialog__head">
            <RDialog.Title className="dialog__title">{title}</RDialog.Title>
            <RDialog.Close asChild>
              <IconButton label="Close">
                <X size={14} />
              </IconButton>
            </RDialog.Close>
          </div>
          {description ? (
            <RDialog.Description className="dialog__desc">{description}</RDialog.Description>
          ) : (
            <RDialog.Description className="sr-only">{title}</RDialog.Description>
          )}
          {children}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

export const Menu = {
  Root: DropdownMenu.Root,
  Trigger: DropdownMenu.Trigger,
  Content: ({ children, align = "start" }: { children: ReactNode; align?: "start" | "end" }) => (
    <DropdownMenu.Portal>
      <DropdownMenu.Content className="menu" align={align} sideOffset={4}>
        {children}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  ),
  Item: ({
    children,
    onSelect,
    disabled,
  }: {
    children: ReactNode;
    onSelect: () => void;
    disabled?: boolean;
  }) => (
    <DropdownMenu.Item className="menu__item" onSelect={onSelect} disabled={disabled}>
      {children}
    </DropdownMenu.Item>
  ),
  Check: ({
    children,
    checked,
    onChange,
  }: {
    children: ReactNode;
    checked: boolean;
    onChange: (checked: boolean) => void;
  }) => (
    <DropdownMenu.CheckboxItem
      className="menu__item menu__item--check"
      checked={checked}
      onCheckedChange={(value) => onChange(value === true)}
      onSelect={(event) => event.preventDefault()}
    >
      <span className="menu__check" aria-hidden>
        {checked ? "✓" : ""}
      </span>
      {children}
    </DropdownMenu.CheckboxItem>
  ),
  Label: ({ children }: { children: ReactNode }) => (
    <DropdownMenu.Label className="menu__label">{children}</DropdownMenu.Label>
  ),
  Separator: () => <DropdownMenu.Separator className="menu__sep" />,
};

export function ErrorBox({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="error-box" role="alert">
      <strong>{title}</strong>
      {detail ? <div className="error-box__detail">{detail}</div> : null}
      {action ? <div className="error-box__action">{action}</div> : null}
    </div>
  );
}

export function Section({
  title,
  aside,
  children,
  id,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section className="section" aria-labelledby={id ? `${id}-title` : undefined}>
      <header className="section__head">
        <h3 className="section__title" id={id ? `${id}-title` : undefined}>
          {title}
        </h3>
        {aside ? <div className="section__aside">{aside}</div> : null}
      </header>
      {children}
    </section>
  );
}
