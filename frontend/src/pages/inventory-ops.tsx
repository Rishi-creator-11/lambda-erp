// Inventory Ops Copilot: public, session-scoped sandbox for the approval-gated reorder workflow.
// Added by Rishi Kumar on top of Lambda ERP (Apache-2.0). Writes here never touch shared ERP data.
import { useCallback, useEffect, useState } from "react";

type Row = {
  item_code: string; item_name: string; on_hand: number; on_order: number; sandbox_on_order: number;
  available: number; reorder_level: number; target_level: number; supplier: string; is_low: boolean; suggested_qty: number;
};
type Stock = { items: Row[]; low_count: number };
type Line = { item_code: string; item_name: string; qty: number; supplier: string; unit_cost: number; line_total: number; justification: string };
type Proposal = { id: string; status: string; lines: Line[]; total: number };
type Audit = { ts: string; event: string; role: string | null; proposal_id: string | null };

function sessionId(): string {
  try {
    let id = localStorage.getItem("inv-ops-sid");
    if (!id) { id = "sess_" + crypto.randomUUID().replace(/-/g, ""); localStorage.setItem("inv-ops-sid", id); }
    return id;
  } catch { return "sess_" + Math.random().toString(36).slice(2).padEnd(20, "x"); }
}

async function call(path: string, body?: unknown) {
  const res = await fetch(`/api/inventory-ops${path}`, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

export default function InventoryOps() {
  const sid = sessionId();
  const [stock, setStock] = useState<Stock | null>(null);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [result, setResult] = useState<{ before: Stock; after: Stock; order_ref: string } | null>(null);
  const [notice, setNotice] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await call(`/state?session_id=${sid}`);
    if (r.ok) { setStock(r.data.stock); setAudit(r.data.audit); }
  }, [sid]);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<void>) { setBusy(true); try { await fn(); } finally { setBusy(false); void load(); } }
  const msg = (r: { ok: boolean; status: number; data: any }, okText: string) =>
    setNotice({ tone: r.ok ? "ok" : "bad", text: r.ok ? okText : `${r.status}: ${r.data.detail ?? "request failed"}` });

  const propose = () => act(async () => {
    const r = await call("/sandbox/propose", { session_id: sid });
    if (r.ok) { setProposal(r.data); setReviewed(false); setResult(null); }
    msg(r, "Draft proposal created. Nothing has been ordered yet.");
  });
  const confirm = (again = false) => act(async () => {
    if (!proposal) return;
    const r = await call("/sandbox/confirm", { session_id: sid, proposal_id: proposal.id, confirm: true });
    if (r.ok) { setResult(r.data); setProposal({ ...proposal, status: "confirmed" }); }
    if (again && !r.ok) setNotice({ tone: "bad", text: `Duplicate blocked (${r.status}): ${r.data.detail}` });
    else msg(r, `Sandbox order ${r.data?.order_ref} recorded for this session only.`);
  });
  const tryReal = () => act(async () => {
    const r = await call("/real/confirm", { proposal_id: proposal?.id ?? "none", confirm: true, session_id: sid });
    setNotice({ tone: r.ok ? "ok" : "bad", text: `Real purchase order attempt as a visitor: ${r.status} - ${r.data.detail ?? "ok"}` });
  });

  return (
    <div className="mx-auto max-w-5xl space-y-6 px-4 py-8 text-sm">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Inventory Ops Copilot</h1>
        <p className="text-muted-foreground">
          Reads inventory, drafts a reorder with quantities and a justification, and writes only after you confirm.
          Built on <a className="underline" href="https://github.com/lambdadevelopment/lambda-erp">Lambda ERP</a> (Apache-2.0).
        </p>
        <p className="inline-block rounded border border-amber-500/50 bg-amber-500/10 px-2 py-1 text-xs">
          Sandbox mode: synthetic print-supply data. Your session's orders are isolated; the shared ERP is never written.
        </p>
      </header>

      {notice && <div role="status" className={`rounded border px-3 py-2 ${notice.tone === "ok" ? "border-green-600/40 bg-green-600/10" : "border-red-600/40 bg-red-600/10"}`}>{notice.text}</div>}

      <section className="space-y-2">
        <h2 className="font-medium">1. Inventory vs reorder level {stock && <span className="text-muted-foreground">({stock.low_count} low)</span>}</h2>
        <div className="overflow-x-auto rounded border">
          <table className="w-full text-left text-xs">
            <thead className="bg-muted/50"><tr>{["Item", "On hand", "On order", "Your sandbox", "Reorder level", "Status"].map((h) => <th key={h} className="px-3 py-2 font-medium">{h}</th>)}</tr></thead>
            <tbody>
              {stock?.items.map((r) => (
                <tr key={r.item_code} className={`border-t ${r.is_low ? "bg-red-600/5" : ""}`}>
                  <td className="px-3 py-2">{r.item_name}<div className="font-mono text-muted-foreground">{r.item_code}</div></td>
                  <td className="px-3 py-2 font-mono">{r.on_hand}</td><td className="px-3 py-2 font-mono">{r.on_order}</td>
                  <td className="px-3 py-2 font-mono">{r.sandbox_on_order}</td><td className="px-3 py-2 font-mono">{r.reorder_level}</td>
                  <td className="px-3 py-2">{r.is_low ? <b className="text-red-600">Low - suggest {r.suggested_qty}</b> : "OK"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <button disabled={busy || !stock?.low_count} onClick={propose} className="rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50">Draft reorder proposal</button>
      </section>

      {proposal && (
        <section className="space-y-2" aria-label="Proposal">
          <h2 className="font-medium">2. Draft proposal <span className="font-mono text-muted-foreground">{proposal.id}</span> - {proposal.status}</h2>
          <ul className="space-y-2">
            {proposal.lines.map((l) => (
              <li key={l.item_code} className="rounded border p-3">
                <div className="font-medium">{l.qty} x {l.item_name} - {l.line_total.toFixed(2)} ({l.supplier})</div>
                <p className="text-xs text-muted-foreground">{l.justification}</p>
              </li>
            ))}
          </ul>
          <div className="font-medium">Total {proposal.total.toFixed(2)} USD (synthetic)</div>
          {proposal.status === "proposed" ? (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2"><input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} /> I reviewed the quantities and justification</label>
              <button disabled={!reviewed || busy} onClick={() => confirm()} className="rounded bg-primary px-3 py-2 text-primary-foreground disabled:opacity-50">Confirm sandbox order</button>
            </div>
          ) : (
            <div className="flex flex-wrap gap-3">
              <button disabled={busy} onClick={() => confirm(true)} className="rounded border px-3 py-2">Try confirming again (duplicate)</button>
            </div>
          )}
          <button disabled={busy} onClick={tryReal} className="rounded border px-3 py-2">Attempt a real purchase order as a visitor</button>
        </section>
      )}

      {result && (
        <section className="space-y-2" aria-label="Before and after">
          <h2 className="font-medium">3. Before and after</h2>
          <div className="grid gap-3 sm:grid-cols-2">
            {([["Before", result.before], ["After", result.after]] as const).map(([label, s]) => (
              <div key={label} className="rounded border p-3">
                <div className="font-medium">{label}: {s.low_count} low item{s.low_count === 1 ? "" : "s"}</div>
                <ul className="mt-1 text-xs">{s.items.filter((r) => r.is_low || r.sandbox_on_order > 0).map((r) => <li key={r.item_code} className="font-mono">{r.item_code}: available {r.available}</li>)}</ul>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="space-y-2">
        <h2 className="font-medium">Audit log (this session)</h2>
        {audit.length === 0 ? <p className="text-muted-foreground">No events yet.</p> : (
          <ul className="space-y-1 font-mono text-xs">{audit.map((a, i) => <li key={i}>{a.ts} - {a.event}{a.proposal_id ? ` (${a.proposal_id})` : ""} - {a.role ?? "system"}</li>)}</ul>
        )}
      </section>
    </div>
  );
}
