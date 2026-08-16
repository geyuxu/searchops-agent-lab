export function ProductArt({ hue, label }: { hue: number; label?: string }) {
  return (
    <div className="product-art" style={{ "--hue": hue } as React.CSSProperties} aria-label="Local placeholder artwork">
      <span>{label?.slice(0, 1).toUpperCase() || "F"}</span>
      <small>LOCAL PLACEHOLDER</small>
    </div>
  )
}

