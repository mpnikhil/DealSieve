import type { DashboardStats, WatchlistItem, OpportunityDetail, OutboundDraft, InboundMessage } from '../types';
import { mockStats, mockWatchlist, mockOpportunityDetail } from '../mock';

const IS_MOCK = import.meta.env.VITE_MOCK === "1";

function approverHeaders(): Record<string, string> {
  const token = localStorage.getItem("dealsieve_approver_token");
  return token ? { "X-DealSieve-Approver": token } : {};
}

export async function fetchStats(): Promise<DashboardStats> {
  if (IS_MOCK) return mockStats;
  const res = await fetch('/api/stats');
  if (!res.ok) throw new Error('Failed to fetch stats');
  return res.json();
}

export async function fetchWatchlist(includeDead = false): Promise<WatchlistItem[]> {
  if (IS_MOCK) {
    if (includeDead) {
      return mockWatchlist;
    }
    return mockWatchlist.filter(item => item.status !== 'DEAD');
  }
  const res = await fetch(`/api/opportunities?include_dead=${includeDead}`);
  if (!res.ok) throw new Error('Failed to fetch watchlist');
  return res.json();
}

export async function fetchOpportunity(id: string): Promise<OpportunityDetail> {
  if (IS_MOCK) {
    if (id === '101' || id === mockOpportunityDetail.opportunity.opportunity_id) {
      return mockOpportunityDetail;
    }
    throw new Error('Not found in mock');
  }
  const res = await fetch(`/api/opportunities/${id}`);
  if (!res.ok) throw new Error('Failed to fetch opportunity');
  return res.json();
}

export async function approveDraft(draftId: string): Promise<OutboundDraft> {
  if (IS_MOCK) {
    const draft = mockOpportunityDetail.drafts.find(d => d.draft_id === draftId);
    if (!draft) throw new Error('Draft not found');
    return { ...draft, status: 'sent', delivery_ref: 'mock_del_ref_123' };
  }
  const res = await fetch(`/api/drafts/${draftId}/approve`, {
    method: 'POST',
    headers: approverHeaders()
  });
  if (!res.ok) throw new Error('Failed to approve draft');
  return res.json();
}

export async function rejectDraft(draftId: string): Promise<OutboundDraft> {
  if (IS_MOCK) {
    const draft = mockOpportunityDetail.drafts.find(d => d.draft_id === draftId);
    if (!draft) throw new Error('Draft not found');
    return { ...draft, status: 'rejected' };
  }
  const res = await fetch(`/api/drafts/${draftId}/reject`, {
    method: 'POST',
    headers: approverHeaders()
  });
  if (!res.ok) throw new Error('Failed to reject draft');
  return res.json();
}

export async function fetchCorrespondence(opportunityId: string): Promise<{ inbound: InboundMessage[], outbound: OutboundDraft[] }> {
  if (IS_MOCK) {
    return {
      inbound: mockOpportunityDetail.inbound_messages,
      outbound: mockOpportunityDetail.drafts
    };
  }
  const res = await fetch(`/api/correspondence/${opportunityId}`);
  if (!res.ok) throw new Error('Failed to fetch correspondence');
  return res.json();
}

export async function tickDiligence(
  asOf: string | null = null,
): Promise<{ follow_ups_sent: number; stalled: number }> {
  if (IS_MOCK) {
    return { follow_ups_sent: 1, stalled: 0 };
  }
  const res = await fetch('/api/diligence/tick', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...approverHeaders() },
    body: JSON.stringify({ as_of: asOf })
  });
  if (!res.ok) throw new Error('Failed to tick diligence');
  return res.json();
}
