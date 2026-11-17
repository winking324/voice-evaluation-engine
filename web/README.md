# Web dashboard

This directory contains the React + TypeScript frontend. API wire DTOs and HTTP
calls are centralized in `src/api.ts`; UI view models and display mapping are
separate. The page uses only the documented same-origin `/api/v1` endpoints. It
does not include mock datasets, transcripts, results, or provider calls.

## Local frontend development

Use Node.js 22.12 or newer. Install dependencies from the lockfile and start the
Vite server:

```bash
npm ci
npm run dev
```

The development server binds to `127.0.0.1:5173` and proxies `/api` to the
FastAPI app at `127.0.0.1:8000`. Start the API separately. When the API is not
available, the dashboard shows an explicit catalog/run error and does not
fabricate data. Metadata reads do not start provider calls; a run is submitted
only after the user selects providers, acknowledges outbound audio/cost, and
presses **Start evaluation**.

## Checks and production assets

```bash
npm run typecheck
npm run build
```

The production bundle is written to `dist/`. The application container should
serve these static assets and `/api/v1` from the same origin, as described in
the containerization design. The frontend package itself does not start a
second production server or contact a third-party service.
