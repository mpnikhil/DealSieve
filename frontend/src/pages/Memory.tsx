import { useEffect, useState, type FormEvent } from 'react';
import { fetchMemory } from '../api';
import type { MemoryHit, MemoryEvent } from '../types';
import { formatRelativeTime } from '../utils/format';
import { cn } from '../components/StatusPill';

export function Memory() {
  const [items, setItems] = useState<MemoryEvent[] | MemoryHit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  const [namespace, setNamespace] = useState("");
  const [query, setQuery] = useState("");
  
  // To avoid triggering fetch on every keystroke, use a form submission or a separate state
  const [activeNamespace, setActiveNamespace] = useState("");
  const [activeQuery, setActiveQuery] = useState("");

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        setLoading(true);
        setError(null);
        const data = await fetchMemory(activeNamespace || undefined, activeQuery || undefined);
        if (active) setItems(data);
      } catch (err: any) {
        if (active) setError(err.message || 'Failed to load memory');
      } finally {
        if (active) setLoading(false);
      }
    }
    load();
    return () => { active = false; };
  }, [activeNamespace, activeQuery]);

  const handleSearch = (e: FormEvent) => {
    e.preventDefault();
    setActiveNamespace(namespace);
    setActiveQuery(query);
  };

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-bold text-slate-900">Decision Memory</h1>
        <p className="text-sm text-slate-500">What DealSieve knows about brokers and your decisions.</p>
      </div>

      <form onSubmit={handleSearch} className="flex gap-4">
        <div className="flex-1">
          <input
            type="text"
            placeholder="Search..."
            className="w-full px-3 py-2 border border-slate-300 rounded focus:ring-blue-500 focus:border-blue-500 text-sm"
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
        </div>
        <div className="w-64">
          <input
            type="text"
            placeholder="investor/ or broker/"
            className="w-full px-3 py-2 border border-slate-300 rounded focus:ring-blue-500 focus:border-blue-500 text-sm"
            value={namespace}
            onChange={e => setNamespace(e.target.value)}
          />
        </div>
        <button
          type="submit"
          className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded hover:bg-blue-700 transition"
        >
          Search
        </button>
      </form>

      {error && <div className="text-red-500 text-sm p-4 bg-red-50 rounded">{error}</div>}
      
      {loading ? (
        <div className="text-slate-500 text-sm py-8">Loading memory...</div>
      ) : items.length === 0 ? (
        <div className="text-slate-500 text-sm py-8 text-center bg-slate-50 rounded border border-slate-200">
          No memories found matching this criteria.
        </div>
      ) : (
        <div className="space-y-4">
          {items.map(item => {
            const isHit = 'score' in item;
            return (
              <div key={item.memory_event_id} className="p-4 bg-white border border-slate-200 shadow-sm rounded-lg flex flex-col gap-2">
                <div className="flex justify-between items-start gap-4">
                  <div className="text-base text-slate-900 font-medium leading-snug">
                    {item.text}
                  </div>
                  <span className={cn(
                    "inline-flex px-1.5 py-0.5 rounded text-[10px] uppercase font-bold tracking-wider shrink-0",
                    item.kind === 'decision' ? 'bg-indigo-100 text-indigo-700' :
                    item.kind === 'broker' ? 'bg-amber-100 text-amber-700' :
                    item.kind === 'alert' ? 'bg-red-100 text-red-700' :
                    'bg-slate-200 text-slate-700'
                  )}>
                    {item.kind}
                  </span>
                </div>
                
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-500 mt-2">
                  <span className="font-mono">{item.namespace}</span>
                  <span>{formatRelativeTime(item.created_at)}</span>
                  {('deal_number' in item && item.deal_number) && (
                    <span>Deal #{item.deal_number}</span>
                  )}
                  {isHit && (
                    <div className="flex items-center gap-1.5 ml-auto">
                      <span className="text-[10px]">Score {((item as MemoryHit).score * 100).toFixed(0)}</span>
                      <div className="w-12 h-1 bg-slate-200 rounded-full overflow-hidden">
                        <div className="h-full bg-blue-400" style={{ width: `${(item as MemoryHit).score * 100}%` }} />
                      </div>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
