"use client";

import { useCallback, useEffect, useState, type CSSProperties } from 'react';
import { apiFetch } from '@/lib/api';
import ThreadDrawer, { ThreadButton } from '@/components/ThreadDrawer';
import {
  displayName, fmtMailDate, fmtSize, splitAddresses, splitQuoted,
  type MailMessage, type SentItem, type SentPage,
} from '@/lib/mailbox';

/**
 * The mailbox's Sent folder, read live from Gmail (GET /sent), newest first.
 *
 * Shows everything the mailbox sent: replies from this app, RFQs, and mail the
 * desk wrote in Gmail itself. Rows are metadata only; opening one loads the body
 * (GET /sent/{id}). Lives in the dashboard, so it uses dashboard.css card classes.
 */

const PAGE = 20;

async function readJson<T>(path: string): Promise<T> {
  const res = await apiFetch(path);
  if (!res.ok) {
    let detail = `Server error ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* non-JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

const chip = (color: string, bg: string): CSSProperties => ({
  fontSize: 11, fontWeight: 600, padding: '2px 8px', borderRadius: 999,
  color, background: bg, whiteSpace: 'nowrap',
});

function SentRow({ item, expanded, onToggle, onThread }: {
  item: SentItem; expanded: boolean; onToggle: () => void; onThread: () => void;
}) {
  const [full, setFull] = useState<MailMessage | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const [showQuoted, setShowQuoted] = useState(false);

  // A row stays mounted when it collapses; reopening it starts folded again.
  const [lastExpanded, setLastExpanded] = useState(expanded);
  if (lastExpanded !== expanded) {
    setLastExpanded(expanded);
    if (!expanded) setShowQuoted(false);
  }

  useEffect(() => {
    if (!expanded || full) return;
    let cancelled = false;
    readJson<MailMessage>(`/sent/${encodeURIComponent(item.id)}`)
      .then(m => { if (!cancelled) { setErrorMsg(''); setFull(m); } })
      .catch((err: unknown) => {
        if (!cancelled) setErrorMsg(err instanceof Error ? err.message : 'Could not load the message');
      });
    return () => { cancelled = true; };
  }, [expanded, full, item.id]);

  const to = displayName(item.to) || '(no recipient)';
  const recipients = splitAddresses(item.to).length + splitAddresses(item.cc).length;
  const { main, quoted } = splitQuoted(full?.body ?? '');

  return (
    <div className={`ecard ${expanded ? 'ecard-open' : ''}`} onClick={onToggle}>
      <div className="ecard-top">
        <div className="ecard-avatar">{(to[0] ?? '?').toUpperCase()}</div>
        <div className="ecard-meta">
          <div className="ecard-sender">To: {to}</div>
          <div className="ecard-addr">
            {recipients > 1 ? `${recipients} recipients` : (item.to.match(/<(.+)>/)?.[1] ?? item.to)}
            {item.date && <span style={{ marginLeft: 8, color: 'var(--faint)' }}>🕐 {fmtMailDate(item.date)}</span>}
          </div>
        </div>
        <div className="ecard-right" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {item.app_reply && <span style={chip('var(--blue-text)', 'var(--blue-tint)')}>Sent from app</span>}
          {item.rfq_reference && <span style={chip('var(--amber)', 'var(--sunken-soft)')}>RFQ {item.rfq_reference}</span>}
        </div>
      </div>
      <div className="ecard-subject">{item.subject || '(no subject)'}</div>
      {!expanded && item.snippet && (
        <div style={{
          marginTop: 4, fontSize: 12, color: 'var(--muted)',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{item.snippet}</div>
      )}
      {expanded && (
        <div className="ecard-body" onClick={e => e.stopPropagation()}>
          {errorMsg
            ? <div role="alert" style={{ fontSize: 13, color: 'var(--red)' }}>⚠️ {errorMsg}</div>
            : !full
              ? <div style={{ color: 'var(--muted)', fontSize: 13, padding: '8px 0' }}>Loading message from Gmail…</div>
              : (
                <>
                  <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8, lineHeight: 1.5 }}>
                    <div><b>To</b> {full.to}</div>
                    {full.cc && <div><b>Cc</b> {full.cc}</div>}
                  </div>
                  <pre className="ecard-pre">{main || '(no text in this message; it may be attachments only)'}</pre>
                  {quoted && (
                    <>
                      <button
                        type="button"
                        onClick={() => setShowQuoted(v => !v)}
                        style={{
                          marginTop: 6, padding: '1px 8px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
                          border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted)',
                        }}
                      >{showQuoted ? 'Hide quoted text' : '··· Show quoted text'}</button>
                      {showQuoted && <pre className="ecard-pre" style={{ color: 'var(--faint)' }}>{quoted}</pre>}
                    </>
                  )}
                  {full.body_clipped && (
                    <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 6 }}>
                      Long message, shortened here. Open it in Gmail for the rest.
                    </div>
                  )}
                  {full.attachments.length > 0 && (
                    <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                      {full.attachments.map((a, i) => (
                        <span key={`${i}-${a.filename}`} style={{
                          fontSize: 12, padding: '3px 8px', borderRadius: 6,
                          background: 'var(--sunken-soft)', color: 'var(--muted-soft)',
                        }}>📎 {a.filename}{a.size_bytes ? ` · ${fmtSize(a.size_bytes)}` : ''}</span>
                      ))}
                    </div>
                  )}
                </>
              )}
          <div style={{ marginTop: 12 }}>
            <ThreadButton onClick={onThread} />
          </div>
        </div>
      )}
    </div>
  );
}

export default function SentFolder() {
  const [items, setItems] = useState<SentItem[]>([]);
  const [nextToken, setNextToken] = useState('');
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);
  const [threadFor, setThreadFor] = useState<string | null>(null);

  const load = useCallback(async (token: string) => {
    setLoading(true);
    setErrorMsg('');
    try {
      const qs = new URLSearchParams({ limit: String(PAGE) });
      if (token) qs.set('page_token', token);
      const page = await readJson<SentPage>(`/sent?${qs}`);
      // A page boundary can shift if mail is sent between loads; drop repeats.
      setItems(prev => {
        const base = token ? prev : [];
        const seen = new Set(base.map(i => i.id));
        return [...base, ...page.items.filter(i => !seen.has(i.id))];
      });
      setNextToken(page.next_page_token || '');
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : 'Could not load the Sent folder');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load('');
  }, [load]);

  return (
    <div className="pane">
      <div className="pane-header">
        <div>
          <h2 className="pane-title">📤 Sent</h2>
          <p className="pane-desc">
            Everything this mailbox has sent, read live from Gmail: replies from the app,
            RFQs, and mail written in Gmail itself. Click a row to read it.
          </p>
        </div>
        <div className="pane-legend">
          <button
            type="button"
            onClick={() => { setExpanded(null); void load(''); }}
            disabled={loading}
            style={{
              padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: 600,
              border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted)',
              cursor: loading ? 'default' : 'pointer',
            }}
          >↻ Reload</button>
        </div>
      </div>

      {items.length === 0 && !loading && !errorMsg && (
        <div className="empty-state">Nothing in the Sent folder yet.</div>
      )}

      <div className="email-list">
        {items.map(item => (
          <SentRow
            key={item.id}
            item={item}
            expanded={expanded === item.id}
            onToggle={() => setExpanded(prev => (prev === item.id ? null : item.id))}
            onThread={() => setThreadFor(item.id)}
          />
        ))}
      </div>

      <div style={{ marginTop: 12, fontSize: 12, color: 'var(--muted)', textAlign: 'center' }}>
        {errorMsg && <div role="alert" style={{ color: 'var(--red)', marginBottom: 8 }}>⚠️ {errorMsg}</div>}
        {loading
          ? <div>Loading from Gmail…</div>
          : (nextToken || (errorMsg && items.length > 0))
            ? (
              <button
                type="button"
                onClick={() => { void load(nextToken); }}
                style={{
                  padding: '7px 16px', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer',
                  border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted-soft)',
                }}
              >{errorMsg ? 'Retry' : 'Load more'}</button>
            )
            : items.length > 0 && <div>End of Sent folder</div>}
        {errorMsg && items.length === 0 && !loading && (
          <button
            type="button"
            onClick={() => { void load(''); }}
            style={{
              padding: '7px 16px', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer',
              border: '1px solid var(--border)', background: 'transparent', color: 'var(--muted-soft)',
            }}
          >Retry</button>
        )}
      </div>

      {threadFor && <ThreadDrawer emailId={threadFor} onClose={() => setThreadFor(null)} />}
    </div>
  );
}
