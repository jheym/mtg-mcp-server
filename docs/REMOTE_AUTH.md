# Remote Deployment with Google Sign-In

Optional OAuth for HTTP deployments (Prefect Horizon, Cloud Run, any host) so a
small group of people can use one hosted server from Claude or another MCP client.
Off by default. stdio is never affected.

## How it works

- MCP clients speak OAuth 2.1 to the server (Protected Resource Metadata, Dynamic
  Client Registration or CIMD, PKCE). Google does not support DCR, so FastMCP's
  `GoogleProvider` runs an OAuth proxy: it registers MCP clients itself, sends the
  user to Google to sign in, and issues its own JWTs.
- A global `AuthMiddleware` admits only verified Google emails in
  `MTG_MCP_AUTH_ALLOWED_EMAILS` (`*` = any verified Google account). Other users
  see no tools and every call fails.
- Scopes are `openid` and `email` only.
- Startup fails if `MTG_MCP_GOOGLE_CLIENT_ID` is set but the secret, base URL, or
  allowlist is missing, so a partial config never starts an open server.

## Setup

### 1. Google OAuth client (any GCP project)

1. **Google Auth Platform → Branding/Audience:** user type *External*, publishing
   status *Testing*. Add every allowed person under **Test users**. In Testing
   mode only test users can sign in at all (a second allowlist layer). With only
   `openid`/`email` scopes, Testing mode does not apply the 7-day refresh-token
   expiry.
2. **Clients → Create client:** type *Web application*.
   - Authorized redirect URI: `<BASE_URL>/auth/callback`
     (for example `https://your-server.fastmcp.app/auth/callback`).
   - Add one redirect URI per deployment URL (production, PR preview, local).
3. Copy the client ID and client secret.

### 2. Server environment

| Variable | Required | Value |
|---|---|---|
| `MTG_MCP_GOOGLE_CLIENT_ID` | yes | OAuth client ID |
| `MTG_MCP_GOOGLE_CLIENT_SECRET` | yes | OAuth client secret |
| `MTG_MCP_AUTH_BASE_URL` | yes | Public URL of this deployment, no trailing `/mcp` |
| `MTG_MCP_AUTH_ALLOWED_EMAILS` | yes | `me@gmail.com,friend@gmail.com` (or `*`) |
| `MTG_MCP_AUTH_JWT_SIGNING_KEY` | no | Random string; default derives from the secret |
| `MTG_MCP_AUTH_REDIS_URL` | no | Persistent OAuth state (needs the `auth-redis` extra) |

On Prefect Horizon, turn the platform's own **Authentication** setting **off**: the
server now does the sign-in itself.

### 3. Connect

In Claude: Settings → Connectors → add a custom connector with `<BASE_URL>/mcp`.
Claude registers, opens the server's consent page, then Google sign-in.

## OAuth state and restarts

The proxy stores client registrations and encrypted upstream tokens. By default
FastMCP uses an encrypted file store on local disk. On hosts with ephemeral disks
(Cloud Run, most PaaS), a restart loses that state and users reconnect the
connector. To avoid that, set `MTG_MCP_AUTH_REDIS_URL` (for example a free Upstash
Redis) and install `mtg-mcp-server[auth-redis]`. Stored values are Fernet-encrypted
with a key derived from the JWT signing key (or the client secret).

## Verify

```bash
curl -i -X POST <BASE_URL>/mcp                       # 401 with resource_metadata
curl <BASE_URL>/.well-known/oauth-protected-resource/mcp
curl <BASE_URL>/.well-known/oauth-authorization-server
```

## Cloud Run

`scripts/deploy_cloud_run.sh` does the whole setup in one run (safe to re-run):
enables the APIs, creates a service account, stores the OAuth client secret and a
generated JWT signing key in Secret Manager, creates a private Cloud Storage bucket
for OAuth state (mounted at `/data`, `FASTMCP_HOME=/data`, so sign-ins survive
scale-to-zero), and deploys from source with the `Dockerfile`.

```bash
PROJECT_ID=my-project \
ALLOWED_EMAILS=me@gmail.com,friend@gmail.com \
GOOGLE_CLIENT_ID=123.apps.googleusercontent.com \
bash scripts/deploy_cloud_run.sh
```

It prints the connector URL and the redirect URI to add to the Google OAuth client
(`https://SERVICE-PROJECT_NUMBER.REGION.run.app/auth/callback`). Defaults:
`us-west1`, 1 GiB, scale to zero, max 1 instance. The service allows
unauthenticated invocation at the Cloud Run layer because MCP clients need OAuth,
not IAM; the server enforces sign-in itself.

Custom domain: map it with Cloud Run domain mapping (Preview; us-west1 supported)
or a load balancer, then re-run the script with `BASE_URL=https://your.domain` and
add `https://your.domain/auth/callback` to the OAuth client.

Prefect Horizon note: Horizon's gateway serves its own `/.well-known` OAuth
endpoints, so self-hosted sign-in needs a plan where that can be turned off.
