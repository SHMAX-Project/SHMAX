/// <reference types="vite/client" />

// BLOCKER 22 — Vite client types. Without this file TypeScript
// cannot resolve ``import.meta.env`` references (used by the
// build for ``VITE_*`` variables).

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
