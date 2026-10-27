/** Identities from ../.env, injected by vite.config.ts under `npm run dev` only (empty otherwise). */
type DevIdentity = { user: string; role: string; tenant: string; token: string };
declare const __DEV_IDENTITIES__: DevIdentity[];
