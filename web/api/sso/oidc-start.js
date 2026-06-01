/**
 * GET /api/sso/oidc-start?tenant=<slug>
 *
 * Initiates an OIDC authorization code flow for enterprise SSO customers.
 * Redirects the browser to the configured IdP (Okta, Azure AD, Google Workspace, etc.)
 *
 * Required env vars (set per-tenant via Vercel env or a tenant config store):
 *   OIDC_ISSUER          -- e.g. https://company.okta.com
 *   OIDC_CLIENT_ID       -- OAuth2 client ID registered in the IdP
 *   OIDC_REDIRECT_URI    -- must match what's registered (e.g. https://getnable.com/api/sso/oidc-callback)
 *
 * Optional env vars:
 *   OIDC_SCOPE           -- defaults to "openid email profile groups"
 *   ACCOUNT_SECRET       -- used to sign the state parameter (CSRF protection)
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

async function hmacHex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export default async function handler(req) {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }

  const url = new URL(req.url);
  const tenant = url.searchParams.get("tenant") || "default";
  // Only allow same-site relative return paths. A return_to of "//evil.com" or
  // "https://evil.com" would otherwise let an attacker hijack the post-login
  // redirect (open redirect → credential phishing).
  // Allow only "/" or "/path" where the char after the leading slash is not "/"
  // or "\" — browsers normalize "/\evil.com" to a protocol-relative "//evil.com",
  // so a naive "startsWith('//')" check is bypassable with a backslash.
  let returnTo = url.searchParams.get("return_to") || "/";
  if (!/^\/($|[^/\\])/.test(returnTo)) returnTo = "/";

  // Load OIDC config from env (single-tenant for now; extend to per-tenant KV later)
  const ISSUER = process.env.OIDC_ISSUER;
  const CLIENT_ID = process.env.OIDC_CLIENT_ID;
  const REDIRECT_URI = process.env.OIDC_REDIRECT_URI || "https://getnable.com/api/sso/oidc-callback";
  const SCOPE = process.env.OIDC_SCOPE || "openid email profile groups";
  const SECRET = process.env.ACCOUNT_SECRET;

  if (!ISSUER || !CLIENT_ID) {
    // SSO not configured — send the user back with a readable message
    // instead of a raw JSON 503 blob in their browser
    return Response.redirect(
      "https://getnable.com/account?error=sso_not_configured",
      302
    );
  }

  // Build a signed state param for CSRF protection
  // state = base64(tenant:returnTo:timestamp):hmac
  const statePayload = btoa(`${tenant}:${returnTo}:${Date.now()}`);
  const stateSig = SECRET
    ? (await hmacHex(SECRET, statePayload)).slice(0, 16)
    : "unsigned";
  const state = `${statePayload}.${stateSig}`;

  // Discover the authorization endpoint from the OIDC discovery document
  // Most IdPs publish /.well-known/openid-configuration
  let authEndpoint;
  try {
    const discovery = await fetch(
      `${ISSUER.replace(/\/$/, "")}/.well-known/openid-configuration`
    );
    if (!discovery.ok) throw new Error(`Discovery failed: ${discovery.status}`);
    const meta = await discovery.json();
    authEndpoint = meta.authorization_endpoint;
  } catch (err) {
    console.error("OIDC discovery error:", err.message);
    return new Response(
      JSON.stringify({ error: "Failed to reach identity provider. Check OIDC_ISSUER." }),
      { status: 502, headers: { "Content-Type": "application/json", ...CORS_HEADERS } }
    );
  }

  const params = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: SCOPE,
    state,
    // Request groups claim in the ID token (works for Okta; Azure uses roles)
    ...(SCOPE.includes("groups") ? {} : {}),
  });

  const redirectUrl = `${authEndpoint}?${params.toString()}`;

  return Response.redirect(redirectUrl, 302);
}
