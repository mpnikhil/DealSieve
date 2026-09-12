import type { OpportunityStatus } from '../types';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

interface StatusPillProps {
  status: OpportunityStatus;
  className?: string;
}

export function StatusPill({ status, className }: StatusPillProps) {
  const colors: Record<OpportunityStatus, string> = {
    DEAD: 'bg-slate-100 text-slate-700 border-slate-200',
    WATCH: 'bg-amber-100 text-amber-700 border-amber-200',
    NEAR: 'bg-orange-100 text-orange-700 border-orange-200',
    REVIEW: 'bg-green-100 text-green-700 border-green-200',
    NEW: 'bg-slate-100 text-slate-700 border-slate-200',
    SCREENING: 'bg-blue-100 text-blue-700 border-blue-200',
  };

  return (
    <span className={cn(
      "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border",
      colors[status] || colors.NEW,
      className
    )}>
      {status}
    </span>
  );
}
