import { useState } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { Overview } from './pages/Overview';
import { DealDetail } from './pages/DealDetail';
import { Memory } from './pages/Memory';
import { Header } from './components/Header';

function App() {
  const [policyVersion, setPolicyVersion] = useState<string | undefined>();

  return (
    <BrowserRouter>
      <div className="min-h-screen flex flex-col">
        <Header policyVersion={policyVersion} />
        <main className="flex-1">
          <Routes>
            <Route path="/" element={<Overview setPolicyVersion={setPolicyVersion} />} />
            <Route path="/deals/:id" element={<DealDetail />} />
            <Route path="/memory" element={<Memory />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

export default App;
