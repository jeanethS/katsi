import type { ButtonHTMLAttributes, PropsWithChildren } from "react";

export function Button({
  children,
  variant = "primary",
  ...props
}: PropsWithChildren<ButtonHTMLAttributes<HTMLButtonElement>> & {
  variant?: "primary" | "ghost" | "danger";
}) {
  return (
    <button className={`button button-${variant}`} type="button" {...props}>
      {children}
    </button>
  );
}

export function Card({ children }: PropsWithChildren) {
  return <section className="card">{children}</section>;
}

export function ProgressBar({ value }: { value: number }) {
  return (
    <div aria-label={`${value}%`} aria-valuemax={100} aria-valuemin={0} aria-valuenow={value} role="progressbar">
      <div style={{ background: "var(--line)", height: 3, overflow: "hidden", borderRadius: 2 }}>
        <div style={{ background: "var(--accent)", height: "100%", width: `${value}%` }} />
      </div>
    </div>
  );
}

export function SourceChip({ name, summary, why }: { name: string; summary: string; why: string }) {
  return (
    <button className="source-chip" type="button">
      {name}
      <span className="source-popover">
        <strong>{summary}</strong>
        {why}
      </span>
    </button>
  );
}
