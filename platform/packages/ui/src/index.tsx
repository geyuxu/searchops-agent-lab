import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from "react"

export function Badge({ children, tone = "neutral" }: PropsWithChildren<{ tone?: "neutral" | "live" | "warn" }>) {
  return <span className={`ui-badge ui-badge--${tone}`}>{children}</span>
}

export function Button({ className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`ui-button ${className}`.trim()} {...props} />
}

export function SourceNotice({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`source-notice${compact ? " source-notice--compact" : ""}`}>
      <span className="source-notice__mark" aria-hidden="true">E</span>
      <div>
        <strong>Know the data</strong>
        <p>Product text + relevance: public Amazon ESCI. Prices, stock, traffic, users + orders: simulated.</p>
      </div>
    </div>
  )
}

export function Metric({ label, value, detail }: { label: string; value: ReactNode; detail?: ReactNode }) {
  return (
    <article className="ui-metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </article>
  )
}

