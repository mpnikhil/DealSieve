import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { fetchStats, fetchWatchlist, tickDiligence } from '../api';
import type { DashboardStats, WatchlistItem } from '../types';
import { StatusPill } from '../components/StatusPill';
import { formatMoney, formatRelativeTime, formatConstraintLabel } from '../utils/format';

export function Overview({ setPolicyVersion }: { setPolicyVersion: (v: string) => void }) {
  const navigate = useNavigate();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [includeDead, setIncludeDead] = useState(false);
  const [ticking, setTicking] = useState(false);
  const [tickResult, setTickResult] = useState<string | null>(null);

  const handleTick = async () => {
    try {
      setTicking(true);
      const res = await tickDiligence();
      setTickResult(
        `Sent ${res.follow_ups_sent} follow-up${res.follow_ups_sent !== 1 ? 's' : ''} · ${res.stalled} stalled`,
      );
      // Refresh list
      const [s, w] = await Promise.all([fetchStats(), fetchWatchlist(includeDead)]);
      setStats(s);
      setWatchlist(w);
      setTimeout(() => setTickResult(null), 5000);
    } catch (err) {
      console.error(err);
      setTickResult("Error running follow-ups");
      setTimeout(() => setTickResult(null), 3000);
    } finally {
      setTicking(false);
    }
  };

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        setLoading(true);
        const [s, w] = await Promise.all([fetchStats(), fetchWatchlist(includeDead)]);
        if (!active) return;
        setStats(s);
        setWatchlist(w);
        setPolicyVersion(s.policy_version);
      } catch (err) {
        console.error(err);
      } finally {
        if (active) setLoading(false);
      }
    }
    load();
    return () => { active = false; };
  }, [includeDead, setPolicyVersion]);

  if (loading && !stats) {
    return <div className="p-8 text-slate-500">Loading dashboard...</div>;
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
      {/* Stat strip */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-7 gap-4">
          <StatCard label="Encountered" value={stats.encountered} />
          <StatCard label="Rejected (DEAD)" value={stats.dead} />
          <StatCard label="Watching" value={stats.watch} />
          <StatCard label="Near Threshold" value={stats.near} highlight={stats.near > 0 ? "orange" : undefined} />
          <StatCard label="Reviewing" value={stats.review} highlight={stats.review > 0 ? "green" : undefined} />
          <StatCard label="Interruptions (7d)" value={stats.human_interruptions_7d} />
          <StatCard label="Awaiting broker" value={stats.open_diligence_requests} />
        </div>
      )}

      {/* Watchlist */}
      <div className="bg-white border border-slate-200 shadow-sm rounded-lg overflow-hidden">
        <div className="px-4 py-4 border-b border-slate-200 flex justify-between items-center bg-slate-50">
          <h2 className="text-base font-semibold text-slate-900 flex items-center gap-3">
            Watchlist
            <button 
              onClick={handleTick}
              disabled={ticking}
              className="text-xs px-2 py-1 bg-white border border-slate-300 rounded text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {ticking ? 'Running...' : 'Run follow-ups'}
            </button>
            {tickResult && <span className="text-xs text-slate-500 font-normal">{tickResult}</span>}
          </h2>
          <label className="flex items-center gap-2 text-sm text-slate-600 cursor-pointer">
            <input 
              type="checkbox" 
              checked={includeDead} 
              onChange={e => setIncludeDead(e.target.checked)}
              className="rounded border-slate-300 text-blue-600 focus:ring-blue-500"
            />
            Include DEAD deals
          </label>
        </div>
        
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-white text-slate-500">
              <tr>
                <th className="px-4 py-3 text-left font-medium w-16">Deal</th>
                <th className="px-4 py-3 text-left font-medium">Name</th>
                <th className="px-4 py-3 text-left font-medium w-24">Status</th>
                <th className="px-4 py-3 text-right font-medium">Asking Price</th>
                <th className="px-4 py-3 text-right font-medium">Max Viable</th>
                <th className="px-4 py-3 text-right font-medium w-32">Distance</th>
                <th className="px-4 py-3 text-left font-medium">Binding Constraints</th>
                <th className="px-4 py-3 text-right font-medium w-32">Updated</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-slate-100">
              {watchlist.map(item => (
                <tr 
                  key={item.opportunity_id}
                  onClick={() => navigate(`/deals/${item.opportunity_id}`)}
                  className="hover:bg-slate-50 cursor-pointer group"
                >
                  <td className="px-4 py-3 text-slate-500">#{item.deal_number}</td>
                  <td className="px-4 py-3 text-slate-900 font-medium truncate max-w-xs" title={item.display_name}>
                    {item.display_name}
                  </td>
                  <td className="px-4 py-3"><StatusPill status={item.status} /></td>
                  <td className="px-4 py-3 text-right font-mono tabular-nums text-slate-700">
                    {formatMoney(item.current_asking_price)}
                  </td>
                  <td className="px-4 py-3 text-right font-mono tabular-nums text-slate-500">
                    {formatMoney(item.max_viable_price)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <DistanceCell item={item} />
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap gap-1">
                      {item.binding_constraints.map(c => (
                        <span key={c} className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-slate-100 text-slate-600">
                          {formatConstraintLabel(c)}
                        </span>
                      ))}
                      {item.binding_constraints.length === 0 && <span className="text-slate-400">—</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-right text-slate-500 text-xs">
                    {formatRelativeTime(item.updated_at)}
                  </td>
                </tr>
              ))}
              {watchlist.length === 0 && (
                <tr>
                  <td colSpan={8} className="px-4 py-8 text-center text-slate-500">No deals found</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value, highlight }: { label: string, value: number, highlight?: 'green' | 'orange' }) {
  let colorClass = "text-slate-900";
  if (highlight === 'green') colorClass = "text-green-600";
  if (highlight === 'orange') colorClass = "text-orange-600";
  
  return (
    <div className="bg-white border border-slate-200 shadow-sm rounded-lg p-4 flex flex-col items-start">
      <div className="text-xs font-medium text-slate-500 mb-1">{label}</div>
      <div className={`text-2xl font-semibold tabular-nums ${colorClass}`}>{value}</div>
    </div>
  );
}

function DistanceCell({ item }: { item: WatchlistItem }) {
  if (item.status === 'REVIEW') {
    return <span className="text-xs font-medium text-green-600/80">passes</span>;
  }
  if (item.max_viable_price === null) {
    return <span className="text-xs italic text-slate-400">no viable price</span>;
  }
  if (item.distance_pct === null) return <span className="text-slate-400">—</span>;

  const pct = Math.max(item.distance_pct, 0);
  const barWidth = (Math.min(pct, 0.5) / 0.5) * 100; // 50% distance fills the bar

  return (
    <div className="flex flex-col items-end gap-1">
      <span className="font-mono tabular-nums text-xs text-slate-700">+{(pct * 100).toFixed(1)}%</span>
      {pct > 0 && (
        <div className="w-16 h-1 bg-slate-100 rounded-full overflow-hidden flex justify-end">
          <div className="h-full bg-orange-400" style={{ width: `${barWidth}%` }} />
        </div>
      )}
    </div>
  );
}
