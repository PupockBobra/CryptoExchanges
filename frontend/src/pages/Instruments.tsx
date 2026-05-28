import { useState, useEffect, useCallback } from 'react'
import { Plus, Pencil, Trash2, X, RefreshCw } from 'lucide-react'
import { EXCHANGES } from '../types'
import type { Instrument } from '../types'

const API = (import.meta.env.VITE_API_URL ?? '') + '/api/instruments'

// ── Helpers ──────────────────────────────────────────────────────────────────

function inferFromCanonical(canonical: string) {
  const isPerp    = canonical.includes(':')
  const base      = canonical.split('/')[0] ?? ''
  const quotePart = canonical.split('/')[1] ?? 'USDT'
  const quote     = quotePart.split(':')[0]
  return { type: isPerp ? 'perp' : 'spot', base_asset: base, quote_asset: quote }
}

// ── Modal form ────────────────────────────────────────────────────────────────

interface FormState {
  canonical:   string
  type:        string
  base_asset:  string
  quote_asset: string
  description: string
  enabled:     boolean
  aliases:     Record<string, string>
}

const BLANK: FormState = {
  canonical: '', type: 'spot', base_asset: '', quote_asset: 'USDT',
  description: '', enabled: true, aliases: {},
}

function toFormState(inst: Instrument): FormState {
  const aliases: Record<string, string> = {}
  for (const ex of EXCHANGES) {
    const v = inst.aliases[ex]
    aliases[ex] = v == null ? '' : v
  }
  return {
    canonical:   inst.canonical,
    type:        inst.type,
    base_asset:  inst.base_asset,
    quote_asset: inst.quote_asset,
    description: inst.description,
    enabled:     inst.enabled,
    aliases,
  }
}

interface ModalProps {
  instrument: Instrument | null   // null = create mode
  onClose: () => void
  onSaved: () => void
}

function InstrumentModal({ instrument, onClose, onSaved }: ModalProps) {
  const [form,    setForm]    = useState<FormState>(instrument ? toFormState(instrument) : BLANK)
  const [saving,  setSaving]  = useState(false)
  const [error,   setError]   = useState('')
  const isEdit = instrument !== null

  // Auto-fill base/quote/type when canonical changes (create mode only)
  const handleCanonical = (val: string) => {
    setForm((f) => {
      const inferred = inferFromCanonical(val)
      return isEdit ? { ...f, canonical: val } : { ...f, canonical: val, ...inferred }
    })
  }

  const setAlias = (ex: string, val: string) =>
    setForm((f) => ({ ...f, aliases: { ...f.aliases, [ex]: val } }))

  const save = async () => {
    if (!form.canonical.trim()) { setError('Canonical symbol is required'); return }
    setSaving(true); setError('')

    // Build aliases object: empty string → omit (use canonical)
    const aliases: Record<string, string | null> = {}
    for (const ex of EXCHANGES) {
      const v = form.aliases[ex]?.trim()
      if (v) aliases[ex] = v
    }

    const body = { ...form, aliases }
    const url  = isEdit ? `${API}/${instrument!.id}` : API
    const method = isEdit ? 'PATCH' : 'POST'

    try {
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail ?? `HTTP ${res.status}`)
      }
      onSaved()
      onClose()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-header">
          <h2>{isEdit ? 'Edit Instrument' : 'Add Instrument'}</h2>
          <button className="btn-icon" onClick={onClose}><X size={16} /></button>
        </div>

        <div className="modal-body">
          {/* Canonical + Type */}
          <div className="form-row">
            <div className="form-group" style={{ gridColumn: '1 / -1' }}>
              <label className="form-label">Canonical symbol *</label>
              <input
                className="form-input"
                placeholder="e.g. BTC/USDT or XAU/USDT:USDT"
                value={form.canonical}
                onChange={(e) => handleCanonical(e.target.value)}
              />
            </div>
          </div>

          <div className="form-row">
            <div className="form-group">
              <label className="form-label">Type</label>
              <select
                className="form-select"
                value={form.type}
                onChange={(e) => setForm((f) => ({ ...f, type: e.target.value }))}
              >
                <option value="spot">Spot</option>
                <option value="perp">Perpetual Future</option>
              </select>
            </div>
            <div className="form-group">
              <label className="form-label">Enabled</label>
              <label className="form-toggle" style={{ marginTop: 6 }}>
                <div
                  className={`toggle ${form.enabled ? 'on' : ''}`}
                  onClick={() => setForm((f) => ({ ...f, enabled: !f.enabled }))}
                />
                <span style={{ fontSize: 13, color: 'var(--muted)' }}>
                  {form.enabled ? 'Active' : 'Paused'}
                </span>
              </label>
            </div>
          </div>

          <div className="form-row">
            <div className="form-group">
              <label className="form-label">Base asset</label>
              <input
                className="form-input"
                placeholder="e.g. BTC"
                value={form.base_asset}
                onChange={(e) => setForm((f) => ({ ...f, base_asset: e.target.value }))}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Quote asset</label>
              <input
                className="form-input"
                placeholder="e.g. USDT"
                value={form.quote_asset}
                onChange={(e) => setForm((f) => ({ ...f, quote_asset: e.target.value }))}
              />
            </div>
          </div>

          <div className="form-group">
            <label className="form-label">Description (optional)</label>
            <input
              className="form-input"
              placeholder="e.g. Bitcoin spot pair"
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            />
          </div>

          {/* Per-exchange aliases */}
          <div>
            <div className="section-label">Exchange symbol overrides</div>
            <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 4, marginBottom: 10 }}>
              Leave blank to use the canonical symbol. Fill in only when an exchange lists this
              instrument under a different ccxt unified name.
            </p>
            <div className="aliases-grid">
              {EXCHANGES.map((ex) => (
                <div key={ex} className="alias-row">
                  <span className="alias-exname">{ex}</span>
                  <input
                    className="form-input"
                    placeholder={form.canonical || 'canonical'}
                    value={form.aliases[ex] ?? ''}
                    onChange={(e) => setAlias(ex, e.target.value)}
                  />
                  <button
                    className="alias-clear"
                    title="Clear override"
                    onClick={() => setAlias(ex, '')}
                  >×</button>
                </div>
              ))}
            </div>
          </div>

          {error && <p className="error">{error}</p>}
        </div>

        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={save} disabled={saving}>
            {saving ? 'Saving…' : isEdit ? 'Save changes' : 'Add instrument'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Delete confirm ────────────────────────────────────────────────────────────

function ConfirmDelete({ instrument, onClose, onDeleted }: { instrument: Instrument; onClose: () => void; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false)

  const confirm = async () => {
    setBusy(true)
    await fetch(`${API}/${instrument.id}`, { method: 'DELETE' })
    onDeleted()
    onClose()
  }

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" style={{ maxWidth: 380 }}>
        <div className="modal-header">
          <h2>Remove instrument?</h2>
          <button className="btn-icon" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="modal-body">
          <p style={{ color: 'var(--muted)', fontSize: 13 }}>
            This will remove <strong style={{ color: 'var(--text)' }}>{instrument.canonical}</strong> from
            tracking. Historical price data is kept in the database.
          </p>
        </div>
        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" style={{ background: 'var(--red)' }} onClick={confirm} disabled={busy}>
            {busy ? 'Removing…' : 'Remove'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Instruments table row ──────────────────────────────────────────────────────

function InstrumentRow({ inst, onEdit, onDelete }: {
  inst:     Instrument
  onEdit:   (i: Instrument) => void
  onDelete: (i: Instrument) => void
}) {
  return (
    <tr>
      <td>
        <span className={`badge badge--${inst.type}`}>
          {inst.type === 'perp' ? 'PERP' : 'SPOT'}
        </span>
      </td>
      <td style={{ fontWeight: 600 }}>{inst.canonical}</td>
      <td>{inst.base_asset}</td>
      <td>{inst.quote_asset}</td>
      <td>
        <div className="aliases-cell">
          {EXCHANGES.map((ex) => {
            const override = inst.aliases[ex]
            const isNull   = override === null
            return (
              <span
                key={ex}
                className={`alias-chip ${override && !isNull ? 'has-override' : ''} ${isNull ? 'na' : ''}`}
                title={isNull ? `${ex}: not available` : override ? `${ex}: ${override}` : `${ex}: ${inst.canonical}`}
              >
                {ex}
              </span>
            )
          })}
        </div>
      </td>
      <td>
        <span className={`badge badge--${inst.enabled ? 'on' : 'off'}`}>
          {inst.enabled ? 'Active' : 'Paused'}
        </span>
      </td>
      <td style={{ color: 'var(--muted)', fontSize: 12 }}>
        {inst.description || '—'}
      </td>
      <td>
        <div className="row-actions">
          <button className="btn-icon" title="Edit" onClick={() => onEdit(inst)}>
            <Pencil size={13} />
          </button>
          <button className="btn-icon danger" title="Remove" onClick={() => onDelete(inst)}>
            <Trash2 size={13} />
          </button>
        </div>
      </td>
    </tr>
  )
}

// ── Page ──────────────────────────────────────────────────────────────────────

type FilterType = 'all' | 'spot' | 'perp'

export function Instruments() {
  const [instruments, setInstruments] = useState<Instrument[]>([])
  const [loading,  setLoading]  = useState(true)
  const [filter,   setFilter]   = useState<FilterType>('all')
  const [editTarget, setEdit]   = useState<Instrument | null | undefined>(undefined) // undefined=closed, null=create
  const [deleteTarget, setDel]  = useState<Instrument | null>(null)
  const [reloading, setReloading] = useState(false)

  const load = useCallback(async () => {
    try {
      const r = await fetch(API)
      setInstruments(await r.json())
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const triggerReload = async () => {
    setReloading(true)
    await fetch(`${API}/reload`, { method: 'POST' })
    setTimeout(() => setReloading(false), 1500)
  }

  const visible = filter === 'all'
    ? instruments
    : instruments.filter((i) => i.type === filter)

  return (
    <div>
      <div className="page-toolbar">
        <h1>Instruments</h1>

        <div className="type-filter">
          {(['all', 'spot', 'perp'] as FilterType[]).map((f) => (
            <button
              key={f}
              className={`filter-btn ${filter === f ? 'filter-btn--active' : ''}`}
              onClick={() => setFilter(f)}
            >
              {f === 'all' ? 'All' : f === 'spot' ? 'Spot' : 'Perpetuals'}
            </button>
          ))}
        </div>

        <button
          className="btn-secondary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={triggerReload}
          title="Signal collector to reload symbol list"
        >
          <RefreshCw size={13} className={reloading ? 'spin' : ''} />
          Reload collector
        </button>

        <button
          className="btn-primary"
          style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          onClick={() => setEdit(null)}
        >
          <Plus size={14} /> Add instrument
        </button>
      </div>

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        {loading ? (
          <p className="empty">Loading…</p>
        ) : visible.length === 0 ? (
          <p className="empty">
            No {filter !== 'all' ? filter : ''} instruments yet.
          </p>
        ) : (
          <table className="instruments-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Symbol</th>
                <th>Base</th>
                <th>Quote</th>
                <th>Exchange aliases</th>
                <th>Status</th>
                <th>Description</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((inst) => (
                <InstrumentRow
                  key={inst.id}
                  inst={inst}
                  onEdit={setEdit}
                  onDelete={setDel}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Spot vs Perp info boxes */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16 }}>
        <div className="card">
          <h3 className="card-title">Spot pairs</h3>
          <p style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.6 }}>
            Spot instruments represent direct ownership of the base asset.
            Symbol format: <code style={{ color: 'var(--accent)' }}>BASE/QUOTE</code> (e.g. <code style={{ color: 'var(--accent)' }}>BTC/USDT</code>).
            Prices reflect real-time buy/sell on exchange spot markets.
          </p>
        </div>
        <div className="card">
          <h3 className="card-title">Perpetual futures</h3>
          <p style={{ fontSize: 13, color: 'var(--muted)', lineHeight: 1.6 }}>
            Perpetual contracts have no expiry date and track the spot index via funding rates.
            Symbol format: <code style={{ color: 'var(--orange)' }}>BASE/QUOTE:SETTLE</code> (e.g. <code style={{ color: 'var(--orange)' }}>XAU/USDT:USDT</code>).
            Prices may diverge slightly from spot — a key source of arbitrage.
          </p>
        </div>
      </div>

      {editTarget !== undefined && (
        <InstrumentModal
          instrument={editTarget}
          onClose={() => setEdit(undefined)}
          onSaved={load}
        />
      )}

      {deleteTarget && (
        <ConfirmDelete
          instrument={deleteTarget}
          onClose={() => setDel(null)}
          onDeleted={load}
        />
      )}
    </div>
  )
}
