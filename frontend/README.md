# DealSieve Dashboard (W5)

This is the DealSieve inspection dashboard, built with Vite, React 18, TypeScript, and Tailwind CSS v4.

## Commands

### Develop with Mock Data
Runs the dev server using rich local fixture data instead of hitting the real backend API.
```bash
VITE_MOCK=1 npm run dev
```

### Develop against Local API
Runs the dev server and proxies `/api` requests to `http://localhost:8000` (the FastAPI backend).
```bash
npm run dev
```

### Build for Production
Compiles the application to static files in `dist/`. The FastAPI backend serves these static files on non-API routes.
```bash
npm run build
```
