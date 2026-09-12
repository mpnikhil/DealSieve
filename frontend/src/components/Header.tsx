import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Info } from 'lucide-react';

export function Header({ policyVersion }: { policyVersion?: string }) {
  const [showPolicy, setShowPolicy] = useState(false);

  return (
    <header className="bg-white border-b border-slate-200 sticky top-0 z-10">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between">
        <Link to="/" className="text-lg font-semibold text-slate-900 tracking-tight">
          DealSieve
        </Link>
        <div className="relative flex items-center gap-2">
          {policyVersion && (
            <span className="text-xs text-slate-500 font-mono tracking-tight">
              {policyVersion}
            </span>
          )}
          <button 
            onMouseEnter={() => setShowPolicy(true)}
            onMouseLeave={() => setShowPolicy(false)}
            className="text-slate-400 hover:text-slate-600 transition-colors"
          >
            <Info className="w-4 h-4" />
          </button>
          
          {showPolicy && (
            <div className="absolute right-0 top-full mt-2 w-64 bg-white border border-slate-200 shadow-lg rounded-md p-3 z-20 text-xs text-slate-700">
              <div className="font-semibold mb-2">Investment policy</div>
              <ul className="space-y-1">
                <li><span className="font-medium">8.0%</span> min normalized cap</li>
                <li><span className="font-medium">1.35x</span> min DSCR</li>
                <li><span className="font-medium">&lt;=25%</span> largest tenant</li>
                <li><span className="font-medium">&gt;=5</span> tenants</li>
                <li><span className="font-medium">75%</span> max LTV</li>
                <li><span className="font-medium">$2.0M</span> max price</li>
              </ul>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
