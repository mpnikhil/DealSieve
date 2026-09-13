import { useState } from 'react';
import type { DiligenceRequest, OutboundDraft, InboundMessage, DocumentAnalysis } from '../types';
import { formatRelativeTime } from '../utils/format';
import { cn } from './StatusPill';
import { Paperclip, X } from 'lucide-react';

const IS_MOCK = import.meta.env.VITE_MOCK === "1";
function getDocumentImageSrc(analysis_id: string, index: number) {
  if (IS_MOCK) {
    return `data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="200" height="150" fill="%23f1f5f9"><rect width="200" height="150"/><text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="14" fill="%2394a3b8">Placeholder ${index}</text></svg>`;
  }
  return `/api/documents/${analysis_id}/images/${index}`;
}

export function DiligencePanel({ requests }: { requests: DiligenceRequest[] }) {
  if (!requests || requests.length === 0) return null;
  const openCount = requests.filter(r => ['draft', 'sent', 'overdue', 'stalled'].includes(r.status)).length;
  const answeredCount = requests.filter(r => r.status === 'answered').length;

  return (
    <div className="bg-white border border-slate-200 rounded-lg overflow-hidden shadow-sm">
      <div className="px-5 py-4 border-b border-slate-200 bg-slate-50 flex justify-between items-center">
        <h2 className="text-base font-semibold text-slate-900">Diligence</h2>
        <span className="text-sm text-slate-500">{openCount} open · {answeredCount} answered</span>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <thead className="bg-white">
            <tr>
              <th className="px-5 py-3 text-left font-medium text-slate-500">Topic</th>
              <th className="px-5 py-3 text-left font-medium text-slate-500">Status</th>
              <th className="px-5 py-3 text-left font-medium text-slate-500">Sent</th>
              <th className="px-5 py-3 text-left font-medium text-slate-500">Due</th>
              <th className="px-5 py-3 text-left font-medium text-slate-500">Follow-ups</th>
              <th className="px-5 py-3 text-left font-medium text-slate-500">Answer</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {requests.map(req => {
              let statusColor = "bg-slate-100 text-slate-700";
              if (req.status === 'sent') statusColor = "bg-blue-100 text-blue-700";
              if (req.status === 'overdue') statusColor = "bg-amber-100 text-amber-700";
              if (req.status === 'answered') statusColor = "bg-green-100 text-green-700";
              if (req.status === 'stalled') statusColor = "bg-red-100 text-red-700";
              if (req.status === 'withdrawn') statusColor = "bg-slate-50 text-slate-400";
              
              return (
                <tr key={req.request_id} className="hover:bg-slate-50">
                  <td className="px-5 py-3 font-medium text-slate-900">{req.topic}</td>
                  <td className="px-5 py-3">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium uppercase tracking-wide ${statusColor}`}>
                      {req.status}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-slate-500">{req.sent_at ? formatRelativeTime(req.sent_at) : '—'}</td>
                  <td className="px-5 py-3 text-slate-500">{req.due_at ? formatRelativeTime(req.due_at) : '—'}</td>
                  <td className="px-5 py-3 text-slate-500">
                    {req.follow_up_count > 0 ? `${req.follow_up_count} of 2` : '—'}
                  </td>
                  <td className="px-5 py-3 text-slate-500 text-xs whitespace-normal break-words max-w-xs">
                    {req.status === 'answered' ? (
                      <div>
                        {req.answer_summary}
                        {req.answered_by_document && (
                          <div className="mt-1">
                            <a href={`#doc-${req.answered_by_document}`} className="text-blue-600 hover:underline">View Document</a>
                          </div>
                        )}
                      </div>
                    ) : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function CorrespondencePanel({ inbound, outbound, onApprove, onReject }: { inbound: InboundMessage[], outbound: OutboundDraft[], onApprove: (id: string) => void, onReject: (id: string, reason?: string) => void }) {
  const items = [
    ...inbound.map(i => ({ type: 'in', date: i.received_at, data: i })),
    ...outbound.map(o => ({ type: 'out', date: o.created_at, data: o }))
  ].sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());

  if (items.length === 0) return null;

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm">
      <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
        <h2 className="text-base font-semibold text-slate-900">Correspondence</h2>
      </div>
      <div className="p-5 relative">
        <div className="absolute left-8 top-5 bottom-5 w-px bg-slate-200 z-0"></div>
        <div className="space-y-6 relative z-10">
          {items.map((item, idx) => (
            <div key={idx} className="flex gap-4">
              <div className="w-6 h-6 rounded-full flex-shrink-0 mt-1 flex items-center justify-center bg-white border border-slate-300 shadow-sm">
                <div className={`w-2 h-2 rounded-full ${item.type === 'in' ? 'bg-slate-400' : 'bg-blue-500'}`}></div>
              </div>
              <div className="flex-1 bg-white border border-slate-200 rounded p-4 shadow-sm">
                {item.type === 'in' ? (
                  <InboundItem msg={item.data as InboundMessage} />
                ) : (
                  <OutboundItem draft={item.data as OutboundDraft} onApprove={onApprove} onReject={onReject} />
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function InboundItem({ msg }: { msg: InboundMessage }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div>
      <div className="flex justify-between items-start mb-2">
        <div>
          <div className="text-sm font-semibold text-slate-900">{msg.sender_name ? `${msg.sender_name} <${msg.sender ?? ''}>` : (msg.sender ?? 'unknown sender')}</div>
          <div className="text-sm text-slate-700">{msg.subject}</div>
        </div>
        <div className="text-xs text-slate-500">{formatRelativeTime(msg.received_at)}</div>
      </div>
      {msg.attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-3">
          {msg.attachments.map((a, i) => (
            <span key={i} className="inline-flex items-center px-2 py-1 bg-slate-100 border border-slate-200 rounded text-xs text-slate-600">
              <Paperclip className="w-3 h-3 mr-1" />
              {a.filename}
            </span>
          ))}
        </div>
      )}
      <div className="text-sm text-slate-600 whitespace-pre-wrap">
        {expanded ? msg.body_text : (msg.body_text.length > 150 ? msg.body_text.substring(0, 150) + '...' : msg.body_text)}
        {msg.body_text.length > 150 && (
          <button onClick={() => setExpanded(!expanded)} className="text-blue-600 hover:underline ml-2 text-xs">
            {expanded ? 'Show less' : 'Read more'}
          </button>
        )}
      </div>
    </div>
  );
}

function OutboundItem({ draft, onApprove, onReject }: { draft: OutboundDraft, onApprove: (id: string) => void, onReject: (id: string, reason?: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  
  let statusText = "";
  if (!draft.requires_approval && draft.status === 'sent') statusText = "Auto-sent under policy";
  else if (draft.status === 'pending') statusText = "Awaiting your approval";
  else if (draft.status === 'sent') statusText = "Approved and sent";
  else if (draft.status === 'rejected') statusText = "Rejected";
  else statusText = "Blocked by policy screen";

  return (
    <div>
      <div className="flex justify-between items-start mb-2">
        <div className="flex items-center gap-2">
          <span className="inline-flex px-2 py-0.5 bg-blue-100 text-blue-800 text-xs font-medium rounded capitalize">
            {draft.kind.replace('_', ' ')}
          </span>
          <span className="text-sm text-slate-500 font-medium">{statusText}</span>
        </div>
        <div className="text-xs text-slate-500">{formatRelativeTime(draft.created_at)}</div>
      </div>
      
      <div className="text-sm text-slate-800 font-medium mb-1">To: {draft.to_email}</div>
      <div className="text-sm text-slate-700 mb-3">Subject: {draft.subject}</div>

      <div className="text-sm text-slate-600 whitespace-pre-wrap bg-slate-50 p-3 rounded border border-slate-100">
        {expanded ? draft.body : (draft.body.length > 150 ? draft.body.substring(0, 150) + '...' : draft.body)}
        {draft.body.length > 150 && (
          <button onClick={() => setExpanded(!expanded)} className="text-blue-600 hover:underline ml-2 text-xs">
            {expanded ? 'Show less' : 'Read more'}
          </button>
        )}
      </div>

      {draft.status === 'pending' && (
        <div className="mt-4">
          {rejecting ? (
            <div className="flex flex-col gap-2">
              <input
                type="text"
                autoFocus
                className="w-full text-sm border-slate-300 rounded focus:ring-blue-500 focus:border-blue-500 p-2 border"
                placeholder="Why? One line, DealSieve will remember it"
                value={rejectReason}
                onChange={e => setRejectReason(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    onReject(draft.draft_id, rejectReason);
                    setRejecting(false);
                  } else if (e.key === 'Escape') {
                    setRejecting(false);
                  }
                }}
              />
              <div className="flex gap-2">
                <button onClick={() => { onReject(draft.draft_id, rejectReason); setRejecting(false); }} className="px-3 py-1.5 bg-red-600 text-white text-sm font-medium rounded hover:bg-red-700 transition">
                  Reject
                </button>
                <button onClick={() => setRejecting(false)} className="px-3 py-1.5 bg-white border border-slate-300 text-slate-700 text-sm font-medium rounded hover:bg-slate-50 transition">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <button onClick={() => onApprove(draft.draft_id)} className="px-3 py-1.5 bg-blue-600 text-white text-sm font-medium rounded hover:bg-blue-700 transition">
                Approve
              </button>
              <button onClick={() => setRejecting(true)} className="px-3 py-1.5 bg-white border border-slate-300 text-slate-700 text-sm font-medium rounded hover:bg-slate-50 transition">
                Reject
              </button>
            </div>
          )}
        </div>
      )}

      {draft.status === 'sent' && draft.delivery_ref && (
        <div className="mt-2 text-xs text-slate-400">Delivery ref: {draft.delivery_ref}</div>
      )}
    </div>
  );
}

export function MemoryPanel({ memories }: { memories: import('../types').MemoryHit[] }) {
  const grouped = memories.reduce((acc, m) => {
    const prefix = m.namespace.startsWith('investor/') ? 'Your decisions' : 
                   m.namespace.startsWith('broker/') ? 'This broker' : 'Other';
    if (!acc[prefix]) acc[prefix] = [];
    acc[prefix].push(m);
    return acc;
  }, {} as Record<string, typeof memories>);

  return (
    <div className="bg-white border border-slate-200 rounded-lg overflow-hidden shadow-sm">
      <div className="px-5 py-4 border-b border-slate-200 bg-slate-50">
        <h2 className="text-base font-semibold text-slate-900">What DealSieve remembers</h2>
      </div>
      <div className="p-5">
        {memories.length === 0 ? (
          <div className="text-sm text-slate-500 italic">
            Nothing yet. Approve or reject something and DealSieve will remember why.
          </div>
        ) : (
          <div className="space-y-6">
            {Object.entries(grouped).map(([group, hits]) => (
              <div key={group}>
                <h3 className="text-sm font-semibold text-slate-900 uppercase tracking-wider mb-3">{group}</h3>
                <div className="space-y-3">
                  {hits.map(hit => (
                    <div key={hit.memory_event_id} className="flex flex-col gap-1.5 p-3 border border-slate-100 bg-slate-50 rounded">
                      <div className="flex justify-between items-start gap-2">
                        <div className="text-sm text-slate-800">{hit.text}</div>
                        <span className={cn(
                          "inline-flex px-1.5 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider shrink-0",
                          hit.kind === 'decision' ? 'bg-indigo-100 text-indigo-700' :
                          hit.kind === 'broker' ? 'bg-amber-100 text-amber-700' :
                          hit.kind === 'alert' ? 'bg-red-100 text-red-700' :
                          'bg-slate-200 text-slate-700'
                        )}>
                          {hit.kind}
                        </span>
                      </div>
                      <div className="flex items-center justify-between text-xs text-slate-500">
                        <span>{formatRelativeTime(hit.created_at)}</span>
                        <div className="flex items-center gap-1.5">
                          <span className="text-[10px]">Score {(hit.score * 100).toFixed(0)}</span>
                          <div className="w-12 h-1 bg-slate-200 rounded-full overflow-hidden">
                            <div className="h-full bg-blue-400" style={{ width: `${hit.score * 100}%` }} />
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function DocumentsPanel({ docs }: { docs: DocumentAnalysis[] }) {
  const [lightboxImg, setLightboxImg] = useState<string | null>(null);

  if (!docs || docs.length === 0) return null;

  return (
    <div className="space-y-4">
      {docs.map(doc => (
        <div key={doc.analysis_id} id={`doc-${doc.analysis_id}`} className="bg-white border border-slate-200 rounded-lg shadow-sm p-5">
          <div className="flex items-start justify-between mb-3">
            <div>
              <div className="flex items-center gap-2 mb-1">
                <span className="inline-flex px-2 py-0.5 bg-indigo-100 text-indigo-800 text-xs font-medium rounded capitalize">
                  {doc.document_type.replace(/_/g, ' ')}
                </span>
                <span className="text-sm font-semibold text-slate-900">{doc.filename}</span>
              </div>
              <p className="text-sm text-slate-600">{doc.summary}</p>
            </div>
          </div>

          <div className="text-xs text-slate-500 mb-4">Images reviewed: {doc.images_reviewed}</div>

          {doc.red_flags.length > 0 && (
            <div className="mb-4">
              <h4 className="text-xs font-semibold text-slate-900 uppercase tracking-wider mb-2">Red Flags</h4>
              <ul className="space-y-1">
                {doc.red_flags.map((flag, i) => (
                  <li key={i} className="text-sm text-red-700 bg-red-50 px-3 py-2 rounded border border-red-100 flex items-start gap-2">
                    <span className="text-red-500 mt-0.5">⚠️</span> {flag}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {doc.capex_items.length > 0 && (
            <div className="mb-4">
              <h4 className="text-xs font-semibold text-slate-900 uppercase tracking-wider mb-2">Capex Items</h4>
              <div className="space-y-2">
                {doc.capex_items.map((item, i) => {
                  const isImmediate = item.urgency === 'immediate' || item.urgency === 'near_term';
                  return (
                    <div key={i} className="flex items-center justify-between bg-slate-50 border border-slate-100 p-2 rounded">
                      <div>
                        <div className="text-sm font-medium text-slate-900 flex items-center gap-2">
                          {item.item}
                          <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded",
                            item.urgency === 'immediate' ? 'bg-red-100 text-red-700' :
                            item.urgency === 'near_term' ? 'bg-orange-100 text-orange-700' :
                            'bg-slate-200 text-slate-700'
                          )}>
                            {item.urgency}
                          </span>
                        </div>
                        {isImmediate && (
                          <div className="text-xs text-amber-600 mt-0.5">Counts toward immediate capex</div>
                        )}
                      </div>
                      <div className="text-sm font-mono text-slate-700">
                        ${item.low.toLocaleString()} - ${item.high.toLocaleString()}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {doc.findings.length > 0 && (
            <div className="mb-4">
              <h4 className="text-xs font-semibold text-slate-900 uppercase tracking-wider mb-2">Findings</h4>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {doc.findings.map((f, i) => (
                  <div key={i} className="border border-slate-200 p-2.5 rounded">
                    <div className="flex justify-between items-start mb-1">
                      <span className="text-sm font-medium text-slate-900">{f.topic}</span>
                      <span className={cn("text-[10px] uppercase font-bold px-1.5 py-0.5 rounded",
                        f.severity === 'high' ? 'bg-red-100 text-red-700' :
                        f.severity === 'medium' ? 'bg-amber-100 text-amber-700' :
                        f.severity === 'low' ? 'bg-yellow-100 text-yellow-700' :
                        'bg-blue-100 text-blue-700'
                      )}>
                        {f.severity}
                      </span>
                    </div>
                    <div className="text-sm text-slate-800 mb-1">{f.value}</div>
                    {f.detail && <div className="text-xs text-slate-500 mb-2">{f.detail}</div>}
                    <div className="flex gap-2 text-xs text-slate-400 font-mono">
                      {f.page !== null && <span>Page {f.page}</span>}
                      {f.image_ref && <span>Photo {doc.image_paths.indexOf(f.image_ref) >= 0 ? doc.image_paths.indexOf(f.image_ref) + 1 : f.image_ref.replace(/^image\s*/i, '')}</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {doc.image_paths.length > 0 && (
            <div>
              <h4 className="text-xs font-semibold text-slate-900 uppercase tracking-wider mb-2">Images</h4>
              <div className="flex overflow-x-auto gap-3 pb-2">
                {doc.image_paths.map((_, i) => {
                  const src = getDocumentImageSrc(doc.analysis_id, i + 1);
                  return (
                    <img 
                      key={i} 
                      src={src} 
                      alt={`Image ${i + 1}`} 
                      className="w-32 h-24 object-cover border border-slate-200 rounded cursor-pointer hover:border-blue-400 hover:shadow-sm"
                      onClick={() => setLightboxImg(src)}
                    />
                  );
                })}
              </div>
            </div>
          )}
        </div>
      ))}

      {lightboxImg && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/80 p-4" onClick={() => setLightboxImg(null)}>
          <button className="absolute top-4 right-4 text-white hover:text-slate-300" onClick={() => setLightboxImg(null)}>
            <X className="w-8 h-8" />
          </button>
          <img src={lightboxImg} className="max-w-full max-h-full object-contain" onClick={e => e.stopPropagation()} />
        </div>
      )}
    </div>
  );
}
