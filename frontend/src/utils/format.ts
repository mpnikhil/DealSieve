import { formatDistanceToNow } from 'date-fns';

export function formatMoney(amount: number | null | undefined): string {
  if (amount == null) return '—';
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(amount);
}

export function formatRate(rate: number | null | undefined): string {
  if (rate == null) return '—';
  return `${(rate * 100).toFixed(2)}%`;
}

export function formatDSCR(dscr: number | null | undefined): string {
  if (dscr == null) return '—';
  return `${dscr.toFixed(2)}x`;
}

export function formatRelativeTime(isoString: string | null | undefined): string {
  if (!isoString) return '—';
  return formatDistanceToNow(new Date(isoString), { addSuffix: true });
}

export function formatConstraintLabel(constraint: string): string {
  const map: Record<string, string> = {
    'min_normalized_cap_rate': 'Cap rate',
    'min_base_dscr': 'DSCR',
    'max_ltv': 'LTV',
    'absolute_max_price': 'Max price',
    'tenant_count_min': 'Min tenants',
    'largest_tenant_pct_max': 'Max single tenant %'
  };
  return map[constraint] || constraint;
}
