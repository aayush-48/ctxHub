// OAuth 2.1 authorization code + PKCE against this server, as the first-party "ctx-web"
// public client. Tokens live in sessionStorage (per tab, cleared when the tab closes).

const CLIENT_ID = "ctx-web";
const KEY = "ctx.tokens";

type Tokens = { access_token: string; refresh_token?: string; expires_at: number };

const redirectUri = () => `${location.origin}/app/callback`;

function b64url(bytes: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function random(n = 32): string {
  return b64url(crypto.getRandomValues(new Uint8Array(n)).buffer);
}

function load(): Tokens | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Tokens) : null;
  } catch {
    return null;
  }
}

function save(t: { access_token: string; refresh_token?: string; expires_in?: number }) {
  const tokens: Tokens = {
    access_token: t.access_token,
    refresh_token: t.refresh_token,
    expires_at: Date.now() + (t.expires_in ?? 3600) * 1000,
  };
  sessionStorage.setItem(KEY, JSON.stringify(tokens));
}

export async function login(returnTo = location.pathname + location.search): Promise<never> {
  const verifier = random(48);
  const challenge = b64url(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)),
  );
  const state = random(16);
  sessionStorage.setItem("ctx.pkce", JSON.stringify({ verifier, state, returnTo }));
  const q = new URLSearchParams({
    response_type: "code", client_id: CLIENT_ID, redirect_uri: redirectUri(),
    code_challenge: challenge, code_challenge_method: "S256", state, scope: "ctx",
  });
  location.assign(`/authorize?${q}`);
  return new Promise(() => {});
}

export async function handleCallback(): Promise<string> {
  const params = new URLSearchParams(location.search);
  const pkce = JSON.parse(sessionStorage.getItem("ctx.pkce") || "{}");
  sessionStorage.removeItem("ctx.pkce");
  if (params.get("error")) throw new Error(`Sign-in failed: ${params.get("error")}`);
  if (!params.get("code") || params.get("state") !== pkce.state) {
    throw new Error("Sign-in could not be verified. Try again.");
  }
  const r = await fetch("/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", code: params.get("code")!,
      redirect_uri: redirectUri(), client_id: CLIENT_ID, code_verifier: pkce.verifier,
    }),
  });
  if (!r.ok) throw new Error("Sign-in failed while exchanging the code.");
  save(await r.json());
  return pkce.returnTo && !pkce.returnTo.startsWith("/app/callback") ? pkce.returnTo : "/app/";
}

async function refresh(t: Tokens): Promise<Tokens | null> {
  if (!t.refresh_token) return null;
  const r = await fetch("/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token", refresh_token: t.refresh_token, client_id: CLIENT_ID,
    }),
  });
  if (!r.ok) return null;
  save(await r.json());
  return load();
}

export async function accessToken(): Promise<string> {
  let t = load();
  if (t && t.expires_at - Date.now() < 60_000) t = await refresh(t);
  if (!t) return login();
  return t.access_token;
}

export function logout() {
  sessionStorage.removeItem(KEY);
  fetch("/logout", { method: "POST" }).finally(() => location.assign("/app/"));
}
