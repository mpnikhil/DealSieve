import type { DashboardStats, WatchlistItem, OpportunityDetail, OutboundDraft } from '../types';
import { mockStats, mockWatchlist, mockOpportunityDetail } from '../mock';

const IS_MOCK = import.meta.env.VITE_MOCK === "1";

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
    return { ...draft, status: 'approved' };
  }
  const res = await fetch(`/api/drafts/${draftId}/approve`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to approve draft');
  return res.json();
}

export async function rejectDraft(draftId: string): Promise<OutboundDraft> {
  if (IS_MOCK) {
    const draft = mockOpportunityDetail.drafts.find(d => d.draft_id === draftId);
    if (!draft) throw new Error('Draft not found');
    return { ...draft, status: 'rejected' };
  }
  const res = await fetch(`/api/drafts/${draftId}/reject`, { method: 'POST' });
  if (!res.ok) throw new Error('Failed to reject draft');
  return res.json();
}
