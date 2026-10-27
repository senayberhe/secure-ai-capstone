/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The browser only talks to this origin; /v1 and /health are proxied to the API,
// so the API never needs CORS. Point VITE_API_TARGET at a non-default API port.
const target = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

type DevIdentity = { user: string; role: string; tenant: string; token: string };

// `npm run dev` only: offer the identities from ../.env as one-click sign-in buttons.
// Builds and tests get an empty list, so no token can reach a bundle that is shipped.
function devIdentities(mode: string, command: string): DevIdentity[] {
  if (command !== "serve" || mode !== "development" || process.env.VITE_DEV_SIGNIN === "0") return [];
  const raw = loadEnv(mode, "..", "CAPSTONE_TOKENS_JSON").CAPSTONE_TOKENS_JSON;
  if (!raw) return [];
  try {
    return Object.entries(JSON.parse(raw) as Record<string, { user: string; role?: string; tenant: string }>)
      .map(([token, v]) => ({ user: v.user, role: v.role ?? "reader", tenant: v.tenant, token }));
  } catch {
    return [];
  }
}

export default defineConfig(({ mode, command }) => ({
  plugins: [react(), tailwindcss()],
  define: { __DEV_IDENTITIES__: JSON.stringify(devIdentities(mode, command)) },
  server: {
    port: 5173,
    proxy: { "/v1": target, "/health": target },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
}));
