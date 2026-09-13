import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ChevronLeft, CheckCircle2, XCircle } from 'lucide-react';
import { fetchOpportunity, approveDraft, rejectDraft } from '../api';
import type { OpportunityDetail } from '../types';
import { StatusPill, cn } from '../components/StatusPill';
import { DiligencePanel, CorrespondencePanel, DocumentsPanel, MemoryPanel } from '../components/DealPanels';
import { formatMoney, formatRate, formatDSCR, formatRelativeTime, formatConstraintLabel } from '../utils/format';

export function DealDetail() {
  const { id } = useParams<{ id: string }>();
  const [detail, setDetail] = useState<OpportunityDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      if (!id) return;
      try {
        setLoading(true);
        const data = await fetchOpportunity(id);
        if (active) setDetail(data);
      } catch (err: any) {
        if (active) setError(err.message || 'Failed to load');
      } finally {
        if (active) setLoading(false);
      }
    }
    load();
    return () => { active = false; };
  }, [id]);

  const handleDraftAction = async (draftId: string, action: 'approve' | 'reject', reason?: string) => {
    if (!detail) return;
    try {
      const updatedDraft = action === 'approve' ? await approveDraft(draftId) : await rejectDraft(draftId, reason);
      setDetail({
        ...detail,
        drafts: detail.drafts.map(d => d.draft_id === draftId ? updatedDraft : d)
      });
    } catch (err) {
      console.error(err);
      alert(`Failed to ${action} draft`);
    }
  };

  if (loading) return <div className="p-8 text-slate-500">Loading deal...</div>;
  if (error || !detail) return <div className="p-8 text-red-500">{error || 'Deal not found'}</div>;

  const { opportunity, property, latest_run, events, evidence, skeptic_reports, drafts, diligence_requests, document_analyses, inbound_messages, notifications, memories } = detail;
  const latestNotification = notifications.length > 0 ? notifications[notifications.length - 1] : null;
  let attentionReason = "none";
  if (latestNotification) {
    if (latestNotification.kind === 'threshold_crossed') attentionReason = "entered REVIEW";
    else if (latestNotification.kind === 'fell_below_threshold') attentionReason = "fell below threshold";
    else if (latestNotification.kind === 'diligence_stalled') attentionReason = "diligence stalled";
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
      {/* Back link */}
      <div>
        <Link to="/" className="inline-flex items-center text-sm text-slate-500 hover:text-slate-800">
          <ChevronLeft className="w-4 h-4 mr-1" /> Back to watchlist
        </Link>
      </div>

      {/* Header */}
      <div className="bg-white border border-slate-200 rounded-lg p-6 shadow-sm">
        <div className="flex flex-col md:flex-row md:justify-between md:items-start gap-4">
          <div>
            <div className="flex items-center gap-3 mb-2">
              <span className="text-sm font-medium text-slate-500">Deal #{opportunity.deal_number}</span>
              <StatusPill status={opportunity.status} />
              {opportunity.human_attention_required && (
                <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">
                  Human attention required: {attentionReason}
                </span>
              )}
            </div>
            <h1 className="text-2xl font-bold text-slate-900 mb-1">{opportunity.display_name}</h1>
            <div className="text-sm text-slate-500">
              {(() => {
                const nameMatches = opportunity.display_name.toLowerCase().includes(property.canonical_address.toLowerCase()) || property.canonical_address.toLowerCase().includes(opportunity.display_name.toLowerCase());
                let sublineStr = property.canonical_address;
                if (nameMatches) {
                  const parts = [];
                  const loc = [
                    property.city && property.state ? `${property.city}, ${property.state}` : (property.city || property.state || ''),
                    property.postal_code || ''
                  ].filter(Boolean).join(' ').trim();
                  if (loc) parts.push(loc);
                  if (property.property_type) parts.push(property.property_type);
                  if (property.building_sqft) parts.push(`${new Intl.NumberFormat('en-US').format(property.building_sqft)} sf`);
                  sublineStr = parts.join(' · ');
                }
                let brokerStr = '';
                if (opportunity.broker_name && opportunity.broker_email) {
                  brokerStr = `Broker: ${opportunity.broker_name} <${opportunity.broker_email}>`;
                } else if (opportunity.broker_name) {
                  brokerStr = `Broker: ${opportunity.broker_name}`;
                } else if (opportunity.broker_email) {
                  brokerStr = `Broker: ${opportunity.broker_email}`;
                }
                return (
                  <div className="flex flex-col gap-0.5">
                    <span>{sublineStr}</span>
                    {brokerStr && <span className="text-slate-400">{brokerStr}</span>}
                  </div>
                );
              })()}
            </div>
            {opportunity.reason_summary && (
              <div className="mt-4 text-sm text-slate-700 p-3 bg-slate-50 border border-slate-100 rounded">
                {opportunity.reason_summary}
              </div>
            )}
          </div>
          <div className="flex flex-col items-end">
            <div className="text-sm text-slate-500 mb-1">Asking Price</div>
            <div className="text-3xl font-semibold text-slate-900 tabular-nums">
              {formatMoney(opportunity.current_asking_price)}
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column (Wider) */}
        <div className="lg:col-span-2 space-y-6">
          
          {/* Comparison */}
          {latest_run && (
            <div className="bg-white border border-slate-200 rounded-lg overflow-hidden shadow-sm">
              <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
                <h2 className="text-base font-semibold text-slate-900">Broker vs DealSieve</h2>
              </div>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-slate-200 text-sm">
                  <thead className="bg-white">
                    <tr>
                      <th className="px-5 py-3 text-left font-medium text-slate-500">Metric</th>
                      <th className="px-5 py-3 text-right font-medium text-slate-500">Broker</th>
                      <th className="px-5 py-3 text-right font-medium text-slate-500">DealSieve</th>
                      <th className="px-5 py-3 text-left font-medium text-slate-500">Note</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 bg-white">
                    {latest_run.comparison.map((row, i) => (
                      <tr key={i} className="hover:bg-slate-50">
                        <td className="px-5 py-2.5 font-medium text-slate-700">{row.metric}</td>
                        <td className="px-5 py-2.5 text-right font-mono tabular-nums text-slate-500">{row.broker || '—'}</td>
                        <td className="px-5 py-2.5 text-right font-mono tabular-nums text-slate-900">{row.dealsieve}</td>
                        <td className="px-5 py-2.5 text-slate-500 text-xs whitespace-normal break-words max-w-xs">{row.note}</td>
                      </tr>
                    ))}
                    {latest_run.comparison.length === 0 && (
                      <tr><td colSpan={4} className="px-5 py-4 text-center text-slate-500">No comparison available</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Skeptic & Drafts */}
          {(skeptic_reports.length > 0 || drafts.length > 0) && (
            <div className="bg-white border border-slate-200 rounded-lg shadow-sm">
              <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
                <h2 className="text-base font-semibold text-slate-900">Skeptic & Due Diligence</h2>
              </div>
              <div className="p-5 space-y-6">
                {skeptic_reports.map(report => (
                  <div key={report.report_id} className="space-y-4">
                    <p className="text-sm text-slate-700 font-medium">{report.summary}</p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      {report.concerns.map((c, i) => (
                        <div key={i} className="border border-slate-200 rounded p-3 bg-white">
                          <div className="flex justify-between items-start mb-2">
                            <span className="font-medium text-sm text-slate-900">{c.topic}</span>
                            <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded", 
                              c.severity === 'high' ? 'bg-red-100 text-red-700' : 'bg-amber-100 text-amber-700'
                            )}>
                              {c.severity}
                            </span>
                          </div>
                          <p className="text-xs text-slate-600 mb-2">{c.why_it_matters}</p>
                          <div className="flex items-center gap-1.5">
                            <span className={cn("text-[10px] px-1.5 py-0.5 rounded border",
                              c.evidence_status === 'missing' ? 'border-red-200 text-red-600 bg-red-50' : 'border-amber-200 text-amber-600 bg-amber-50'
                            )}>
                              {c.evidence_status}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}


              </div>
            </div>
          )}

          <MemoryPanel memories={memories || []} />

          {/* New Panels */}
          <DiligencePanel requests={diligence_requests || []} />
          
          <CorrespondencePanel 
            inbound={inbound_messages || []} 
            outbound={drafts || []} 
            onApprove={(id) => handleDraftAction(id, 'approve')} 
            onReject={(id, reason) => handleDraftAction(id, 'reject', reason)} 
          />
          
          <DocumentsPanel docs={document_analyses || []} />

          {/* Evidence */}
          <div className="bg-white border border-slate-200 rounded-lg overflow-hidden shadow-sm">
            <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
              <h2 className="text-base font-semibold text-slate-900">Evidence</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-200 text-sm">
                <thead className="bg-white">
                  <tr>
                    <th className="px-5 py-3 text-left font-medium text-slate-500">Field</th>
                    <th className="px-5 py-3 text-left font-medium text-slate-500">Value</th>
                    <th className="px-5 py-3 text-left font-medium text-slate-500">Document</th>
                    <th className="px-5 py-3 text-left font-medium text-slate-500">Confidence</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 bg-white">
                  {[...evidence].sort((a, b) => {
                    if (a.field !== b.field) return a.field.localeCompare(b.field);
                    return a.source_document.localeCompare(b.source_document);
                  }).map(ev => (
                    <tr key={ev.evidence_id}>
                      <td className="px-5 py-3 font-medium text-slate-700">{ev.field}</td>
                      <td className="px-5 py-3 text-slate-900">{formatEvidenceValue(ev.field, ev.value)}</td>
                      <td className="px-5 py-3">
                        <div className="text-slate-700">{ev.source_document}</div>
                        {ev.location && <div className="text-xs text-slate-500">{ev.location}</div>}
                        {ev.quote && <div className="text-xs text-slate-400 italic mt-1">"{ev.quote}"</div>}
                      </td>
                      <td className="px-5 py-3 text-slate-500">{(ev.confidence * 100).toFixed(0)}%</td>
                    </tr>
                  ))}
                  {evidence.length === 0 && (
                    <tr><td colSpan={4} className="px-5 py-4 text-center text-slate-500">No evidence recorded</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

        </div>

        {/* Right Column (Sidebar) */}
        <div className="space-y-6">
          
          {/* Viability Frontier */}
          {opportunity.viability && (
            <div className="bg-white border border-slate-200 rounded-lg p-5 shadow-sm">
              <h2 className="text-base font-semibold text-slate-900 mb-4">Viability Frontier</h2>
              
              <div className="space-y-4">
                <div className="flex justify-between items-center">
                  <span className="text-sm text-slate-500">Current Price</span>
                  <span className="font-mono tabular-nums text-slate-900">{formatMoney(opportunity.viability.current_price)}</span>
                </div>
                
                <div className="flex justify-between items-center">
                  <span className="text-sm text-slate-500">Max Viable Price</span>
                  <span className="font-mono tabular-nums font-semibold text-slate-900">{formatMoney(opportunity.viability.max_viable_price)}</span>
                </div>
                
                {opportunity.viability.distance_pct !== null && (
                  <div className="flex flex-col gap-1.5 mt-2">
                    <div className="flex justify-between items-center text-sm">
                      <span className="text-slate-500">Distance</span>
                      <span className={cn("font-mono tabular-nums", opportunity.viability.distance_pct < 0 ? "text-green-600" : "text-slate-900")}>
                        {opportunity.viability.distance_pct < 0 ? '' : '+'}{(opportunity.viability.distance_pct * 100).toFixed(1)}%
                      </span>
                    </div>
                    {opportunity.viability.distance_pct > 0 && (
                      <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                        <div className="h-full bg-orange-400 rounded-full" style={{ width: `${Math.min(opportunity.viability.distance_pct * 100 * 2, 100)}%` }} />
                      </div>
                    )}
                  </div>
                )}
                
                {opportunity.viability.binding_constraints.length > 0 && (
                  <div className="pt-3 border-t border-slate-100">
                    <div className="text-xs text-slate-500 mb-2">Binding Constraints</div>
                    <div className="flex flex-wrap gap-1.5">
                      {opportunity.viability.binding_constraints.map(c => (
                        <span key={c} className="px-2 py-0.5 bg-slate-100 text-slate-600 text-xs rounded border border-slate-200">
                          {formatConstraintLabel(c)}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                
                {opportunity.viability.max_viable_price && opportunity.status !== 'REVIEW' && (
                  <div className="mt-4 text-sm text-slate-600 bg-blue-50 text-blue-800 p-3 rounded border border-blue-100">
                    Viable below {formatMoney(opportunity.viability.max_viable_price)}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Gates */}
          {latest_run && (
            <div className="bg-white border border-slate-200 rounded-lg overflow-hidden shadow-sm">
              <div className="px-5 py-3 border-b border-slate-200 bg-slate-50">
                <h2 className="text-base font-semibold text-slate-900">Gates</h2>
              </div>
              <div className="divide-y divide-slate-100">
                {latest_run.gates.map((g, i) => (
                  <div key={i} className="p-4 flex items-start gap-3">
                    <div className="mt-0.5">
                      {g.passed ? <CheckCircle2 className="w-5 h-5 text-green-500" /> : <XCircle className="w-5 h-5 text-red-500" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="text-sm font-medium text-slate-900 truncate" title={g.description}>{formatGateKey(g.gate, g.threshold)}</span>
                        <span className="text-[10px] uppercase text-slate-500 bg-slate-100 px-1.5 py-0.5 rounded shrink-0">{g.kind}</span>
                      </div>
                      <div className="text-xs text-slate-500 font-mono flex items-center justify-between">
                        <span>Actual: <span className={cn("font-medium", g.passed ? "text-slate-700" : "text-red-600")}>{formatGateValue(g.gate, g.actual)}</span></span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Timeline */}
          <div className="bg-white border border-slate-200 rounded-lg p-5 shadow-sm">
            <h2 className="text-base font-semibold text-slate-900 mb-4">Timeline</h2>
            <div className="space-y-4">
              {[...events].reverse().map((ev, i, arr) => (
                <div key={ev.event_id} className="relative pl-4">
                  {i !== arr.length - 1 && <div className="absolute left-[7px] top-5 bottom-[-16px] w-[2px] bg-slate-200" />}
                  <div className="absolute left-0 top-1.5 w-[16px] h-[16px] rounded-full bg-white border-2 border-slate-300 z-10" />
                  <div className="text-xs text-slate-500 mb-0.5 flex justify-between">
                    <span>{ev.type}</span>
                    <span>{formatRelativeTime(ev.occurred_at)}</span>
                  </div>
                  <div className="text-sm text-slate-900">{ev.summary}</div>
                  <div className="text-xs text-slate-400 mt-0.5 capitalize">{ev.actor}</div>
                </div>
              ))}
              {events.length === 0 && <div className="text-sm text-slate-500 text-center py-2">No events recorded</div>}
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}

function formatGateValue(gate: string, value: number | null): string {
  if (value === null) return '—';
  if (gate.includes('rate') || gate.includes('pct') || gate.includes('ltv')) return formatRate(value);
  if (gate.includes('dscr')) return formatDSCR(value);
  if (gate.includes('price')) return formatMoney(value);
  return String(value);
}

function formatGateKey(gate: string, threshold: number | null): string {
  const t = formatGateValue(gate, threshold);
  switch (gate) {
    case 'tenant_count_min': return `Min ${t} tenants`;
    case 'largest_tenant_pct_max': return `Max ${t} single tenant`;
    case 'absolute_max_price': return `Max price ${t}`;
    case 'max_ltv': return `Max LTV ${t}`;
    case 'min_normalized_cap_rate': return `Min normalized cap ${t}`;
    case 'min_base_dscr': return `Min DSCR ${t}`;
    default: return gate.replace(/_/g, ' ');
  }
}

function formatEvidenceValue(field: string, value: any): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'number') {
    const f = field.toLowerCase();
    if (f.includes('price') || f.includes('noi') || f.includes('income') || f.includes('expenses') || f.includes('rent')) {
      return formatMoney(value);
    }
    if (f.endsWith('_pct') || f.includes('cap_rate')) {
      return value <= 1 ? formatRate(value) : formatRate(value / 100);
    }
    if (f.includes('sqft') || f.includes('count')) {
      return new Intl.NumberFormat('en-US').format(value);
    }
  }
  return String(value);
}
