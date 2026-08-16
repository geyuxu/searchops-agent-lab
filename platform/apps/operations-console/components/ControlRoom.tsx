"use client"

import { Badge, Button, Metric, SourceNotice } from "@searchops/ui"
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react"

type Strategy = { id: string; version: number; name: string; status: string; config: StrategyConfig; created_by: string; created_at: string }
type StrategyConfig = {
  synonyms: Record<string, string[]>
  rewrite_rules: { match: string; rewrite: string }[]
  pinned_product_ids: string[]
  blocked_product_ids: string[]
  brand_boosts: Record<string, number>
  field_weights: Record<string, number>
  minimum_score: number
}
type Metrics = { search_requests: number; p50_latency_ms: number; p95_latency_ms: number; zero_result_requests: number; zero_result_rate: number }
type Row = Record<string, unknown>

const baseline: StrategyConfig = {
  synonyms: {}, rewrite_rules: [], pinned_product_ids: [], blocked_product_ids: [], brand_boosts: {},
  field_weights: { title: 4, brand: 2.5, bullet_point: 1.5, description: 1, category: 1.2 }, minimum_score: 0
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/searchops/${path}`, { cache: "no-store", ...init })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.message || `${path} failed (${response.status})`)
  return body as T
}

function idem(action: string) { return `${action}-${crypto.randomUUID()}` }
function body(value: unknown, key?: string): RequestInit {
  return { method: "POST", headers: { "content-type": "application/json", ...(key ? { "idempotency-key": key } : {}) }, body: JSON.stringify(value) }
}

export function ControlRoom() {
  const [tab, setTab] = useState<"overview" | "strategy" | "audit">("overview")
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [current, setCurrent] = useState<Strategy | null>(null)
  const [history, setHistory] = useState<Strategy[]>([])
  const [zeros, setZeros] = useState<Row[]>([])
  const [low, setLow] = useState<Row[]>([])
  const [popular, setPopular] = useState<Row[]>([])
  const [audits, setAudits] = useState<Row[]>([])
  const [runs, setRuns] = useState<Row[]>([])
  const [query, setQuery] = useState("wireless headphones")
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null)
  const [draftJson, setDraftJson] = useState(JSON.stringify(baseline, null, 2))
  const [working, setWorking] = useState<Strategy | null>(null)
  const [approvalToken, setApprovalToken] = useState("")
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const [m, c, h, z, l, p, a, r] = await Promise.all([
        api<Metrics>("ops/metrics"), api<Strategy>("strategies/current"), api<Strategy[]>("strategies"),
        api<Row[]>("ops/zero-results?limit=12"), api<Row[]>("ops/low-quality?limit=12"),
        api<Row[]>("ops/popular-queries?limit=12"), api<Row[]>("audit?limit=30"), api<Row[]>("ops/evaluation-runs?limit=8")
      ])
      setMetrics(m); setCurrent(c); setHistory(h); setZeros(z); setLow(l); setPopular(p); setAudits(a); setRuns(r)
      if (!working) setDraftJson(JSON.stringify(c.config, null, 2))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Control room unavailable")
    } finally {
      setLoading(false)
    }
  }, [working])

  useEffect(() => { void refresh() }, [refresh])

  async function runPreview(event?: FormEvent) {
    event?.preventDefault()
    setError(""); setMessage("Running dry-run preview…")
    try {
      const config = JSON.parse(draftJson) as StrategyConfig
      const result = await api<Record<string, unknown>>("strategies/preview", body({ query, locale: "en-US", size: 10, config }))
      setPreview(result); setMessage("Dry run complete. No live state changed.")
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Preview failed"); setMessage("") }
  }

  async function createDraft() {
    setError("")
    try {
      const config = JSON.parse(draftJson) as StrategyConfig
      const created = await api<Strategy>("strategies", body({ name: `Operator experiment ${new Date().toISOString().slice(0, 16)}`, actor: "operator@local", request_id: crypto.randomUUID(), config }, idem("create")))
      setWorking(created); setMessage(`Draft v${created.version} created.`); await refresh()
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Draft creation failed") }
  }

  async function transition(action: "submit" | "approve" | "publish") {
    if (!working) return
    setError("")
    try {
      if (action === "publish") {
        const published = await api<Strategy>(`strategies/${working.id}/publish`, body({ actor: "operator@local", request_id: crypto.randomUUID(), approval_token: approvalToken }, idem("publish")))
        setWorking(published); setMessage(`Strategy v${published.version} is live. Search queries now compile this policy.`)
      } else if (action === "approve") {
        const approved = await api<{ strategy: Strategy; approval_token: string }>(`strategies/${working.id}/approve`, body({ actor: "approver@local", request_id: crypto.randomUUID() }, idem("approve")))
        setWorking(approved.strategy); setApprovalToken(approved.approval_token); setMessage(`Strategy v${approved.strategy.version} approved. Token bound to this version.`)
      } else {
        const submitted = await api<Strategy>(`strategies/${working.id}/submit`, body({ actor: "operator@local", request_id: crypto.randomUUID() }, idem("submit")))
        setWorking(submitted); setMessage(`Strategy v${submitted.version} submitted for approval.`)
      }
      await refresh()
    } catch (reason) { setError(reason instanceof Error ? reason.message : `${action} failed`) }
  }

  async function rollback(targetVersion: number) {
    setError("")
    try {
      const rolled = await api<Strategy>("strategies/rollback", body({ target_version: targetVersion, actor: "operator@local", request_id: crypto.randomUUID(), approval_token: approvalToken }, idem("rollback")))
      setWorking(rolled); setMessage(`Rolled back content from v${targetVersion} as governed v${rolled.version}.`); await refresh()
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Rollback failed") }
  }

  const previewSummary = useMemo(() => {
    if (!preview) return null
    const currentPreview = preview.current as { products?: { product_id: string; title: string }[] } | undefined
    const proposed = preview.proposed as { products?: { product_id: string; title: string }[] } | undefined
    const changed = preview.changed_positions as Row[] | undefined
    return { current: currentPreview?.products || [], proposed: proposed?.products || [], changed: changed || [] }
  }, [preview])

  return (
    <main className="control-room">
      <aside className="ops-nav">
        <span className="nav-label">WORKSPACES</span>
        <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}><i>01</i>Overview</button>
        <button className={tab === "strategy" ? "active" : ""} onClick={() => setTab("strategy")}><i>02</i>Strategy lab</button>
        <button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}><i>03</i>Audit ledger</button>
        <div className="nav-foot"><SourceNotice compact /></div>
      </aside>

      <section className="ops-content">
        <div className="ops-titlebar">
          <div><span>COMMERCE SEARCH / US-EN</span><h1>{tab === "overview" ? "Operations pulse" : tab === "strategy" ? "Strategy governance" : "Immutable audit ledger"}</h1></div>
          <button onClick={refresh}>{loading ? "SYNCING…" : "REFRESH ↻"}</button>
        </div>
        {error ? <div className="ops-alert ops-alert--error"><strong>Attention</strong>{error}</div> : null}
        {message ? <div className="ops-alert"><strong>Update</strong>{message}</div> : null}

        {tab === "overview" ? (
          <>
            <div className="metrics-grid">
              <Metric label="Search requests · 24h" value={(metrics?.search_requests || 0).toLocaleString()} detail="Observed, not estimated" />
              <Metric label="P50 latency" value={`${Number(metrics?.p50_latency_ms || 0).toFixed(0)} ms`} detail="Search service wall time" />
              <Metric label="P95 latency" value={`${Number(metrics?.p95_latency_ms || 0).toFixed(0)} ms`} detail="Search service wall time" />
              <Metric label="Zero-result rate" value={`${(Number(metrics?.zero_result_rate || 0) * 100).toFixed(2)}%`} detail={`${metrics?.zero_result_requests || 0} requests`} />
            </div>
            <div className="two-column">
              <article className="ops-panel">
                <PanelHead title="Query inspector" code="LIVE PREVIEW" />
                <form className="query-form" onSubmit={runPreview}><input value={query} onChange={e => setQuery(e.target.value)} /><Button type="submit">Inspect query</Button></form>
                {previewSummary ? <RankCompare summary={previewSummary} /> : <div className="empty-chart"><span>⌁</span><p>Run a query to compare live and proposed rankings.</p></div>}
              </article>
              <article className="ops-panel">
                <PanelHead title="Current strategy" code={current ? `V${current.version} / ${current.status}` : "WAITING"} />
                <div className="strategy-card"><div><Badge tone="live">PUBLISHED</Badge><h3>{current?.name || "Loading baseline"}</h3><p>Created by {current?.created_by || "—"}</p></div><strong>v{current?.version || "—"}</strong></div>
                <pre>{JSON.stringify(current?.config || baseline, null, 2)}</pre>
              </article>
            </div>
            <div className="three-column">
              <QueryTable title="Popular queries" rows={popular} value="requests" onSelect={setQuery} />
              <QueryTable title="Zero-result queue" rows={zeros} value="requests" onSelect={setQuery} />
              <QueryTable title="Low NDCG@10" rows={low} value="ndcg10" onSelect={setQuery} />
            </div>
            <article className="ops-panel runs-panel"><PanelHead title="Real offline evaluation runs" code="ESCI LABELS" />{runs.length ? <SimpleTable rows={runs} /> : <p className="empty-copy">No evaluation has been run yet. `make evaluate` will populate this table with measured output.</p>}</article>
          </>
        ) : null}

        {tab === "strategy" ? (
          <div className="strategy-workspace">
            <article className="ops-panel editor-panel">
              <PanelHead title="Policy document" code="JSON / VALIDATED" />
              <label>Preview query<input value={query} onChange={e => setQuery(e.target.value)} /></label>
              <label>Strategy configuration<textarea data-testid="strategy-editor" spellCheck={false} value={draftJson} onChange={e => setDraftJson(e.target.value)} /></label>
              <div className="button-row"><Button onClick={() => runPreview()}>Dry-run preview</Button><Button data-testid="create-strategy" onClick={createDraft}>Create draft</Button></div>
            </article>
            <article className="ops-panel workflow-panel">
              <PanelHead title="Governed release" code="APPROVAL REQUIRED" />
              <div className="workflow-line"><span className={working ? "done" : "active"}>1</span><div><strong>Create draft</strong><small>{working ? `v${working.version} · ${working.status}` : "Awaiting a policy document"}</small></div></div>
              <div className="workflow-line"><span className={working?.status === "IN_REVIEW" || working?.status === "APPROVED" || working?.status === "PUBLISHED" ? "done" : ""}>2</span><div><strong>Submit review</strong><small>Freezes the draft for approval</small></div><Button data-testid="submit-strategy" disabled={working?.status !== "DRAFT"} onClick={() => transition("submit")}>Submit</Button></div>
              <div className="workflow-line"><span className={working?.status === "APPROVED" || working?.status === "PUBLISHED" ? "done" : ""}>3</span><div><strong>Approve</strong><small>Produces a version-bound token</small></div><Button data-testid="approve-strategy" disabled={working?.status !== "IN_REVIEW"} onClick={() => transition("approve")}>Approve</Button></div>
              <div className="workflow-line"><span className={working?.status === "PUBLISHED" ? "done" : ""}>4</span><div><strong>Publish</strong><small>Live queries compile the new policy</small></div><Button data-testid="publish-strategy" disabled={working?.status !== "APPROVED" || !approvalToken} onClick={() => transition("publish")}>Publish</Button></div>
              <div className="token-box"><span>APPROVAL TOKEN</span><code>{approvalToken || "generated only after approval"}</code></div>
              <PanelHead title="Version history" code={`${history.length} SNAPSHOTS`} />
              <div className="version-list">{history.map(item => <div key={item.id}><span>v{item.version}</span><strong>{item.name}</strong><Badge tone={item.status === "PUBLISHED" ? "live" : "neutral"}>{item.status}</Badge>{current && item.version !== current.version ? <button disabled={!approvalToken || working?.status !== "PUBLISHED"} onClick={() => rollback(item.version)}>Rollback</button> : null}</div>)}</div>
            </article>
            {previewSummary ? <article className="ops-panel preview-wide"><PanelHead title="Dry-run impact" code={`${previewSummary.changed.length} POSITION CHANGES`} /><RankCompare summary={previewSummary} /></article> : null}
          </div>
        ) : null}

        {tab === "audit" ? <article className="ops-panel audit-panel"><PanelHead title="Append-only policy events" code={`${audits.length} LATEST`} /><SimpleTable rows={audits} /></article> : null}
      </section>
    </main>
  )
}

function PanelHead({ title, code }: { title: string; code: string }) {
  return <header className="panel-head"><h2>{title}</h2><span>{code}</span></header>
}

function QueryTable({ title, rows, value, onSelect }: { title: string; rows: Row[]; value: string; onSelect: (query: string) => void }) {
  return <article className="ops-panel query-table"><PanelHead title={title} code={`${rows.length} ROWS`} />{rows.length ? rows.map((row, index) => <button key={index} onClick={() => onSelect(String(row.query || ""))}><span>{String(row.query || "—")}</span><strong>{typeof row[value] === "number" ? Number(row[value]).toFixed(value === "ndcg10" ? 3 : 0) : String(row[value] ?? "—")}</strong></button>) : <p className="empty-copy">No measured rows yet.</p>}</article>
}

function RankCompare({ summary }: { summary: { current: { product_id: string; title: string }[]; proposed: { product_id: string; title: string }[]; changed: Row[] } }) {
  return <div className="rank-compare"><div><span>LIVE</span>{summary.current.slice(0, 5).map((item, i) => <p key={item.product_id}><i>{i + 1}</i>{item.title}</p>)}</div><div><span>PROPOSED</span>{summary.proposed.slice(0, 5).map((item, i) => <p key={item.product_id}><i>{i + 1}</i>{item.title}</p>)}</div></div>
}

function SimpleTable({ rows }: { rows: Row[] }) {
  if (!rows.length) return <p className="empty-copy">No records yet.</p>
  const keys = Object.keys(rows[0]).slice(0, 8)
  return <div className="table-scroll"><table><thead><tr>{keys.map(key => <th key={key}>{key.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{keys.map(key => <td key={key}>{typeof row[key] === "object" && row[key] !== null ? JSON.stringify(row[key]) : String(row[key] ?? "—")}</td>)}</tr>)}</tbody></table></div>
}

