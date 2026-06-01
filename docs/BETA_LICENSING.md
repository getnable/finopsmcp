# Beta licensing (handing out free Team access)

Quick reference for giving testers full Pro/Team access without payment.

## How a license works

A key is **data + a signature**:
- Data: `{email, issue date, plan: "pro"}` (the base64 blob in the middle).
- Signature: `HMAC-SHA256(secret, data)` (the part after the last dash).

HMAC is symmetric: the **same secret** signs the key and verifies it. nable checks
the license on the tester's own machine (`check_license()` runs locally), so that
machine needs the secret to verify the key. The secret is not shipped with the
package, so for a beta the tester supplies both values.

That is why a tester sets **two** env vars:
- `FINOPS_LICENSE_SECRET` — the signing secret, so their machine can verify.
- `FINOPS_LICENSE_KEY` — their individual key, the thing being verified.

## Tester setup

In the MCP client config (e.g. `claude_desktop_config.json`), add both to the
`env` block alongside cloud credentials:

```json
{
  "mcpServers": {
    "finops": {
      "command": "finops-mcp",
      "env": {
        "FINOPS_LICENSE_SECRET": "<beta secret>",
        "FINOPS_LICENSE_KEY": "<their key>",
        "AWS_ACCESS_KEY_ID": "...",
        "AWS_SECRET_ACCESS_KEY": "..."
      }
    }
  }
}
```

Restart the client. They are on full Team/Pro end to end.

The live secret and the per-tester keys are in `beta-licenses.txt` (gitignored,
never committed). Regenerate with:

```bash
FINOPS_LICENSE_SECRET="<secret>" python -c \
  "from finops.license import generate_key; print(generate_key('person@company.com'))"
```

## Notes

- Both env vars are required. The key alone will not validate without the matching
  secret on the same machine.
- Keys expire ~1 year from their issue date (`_KEY_TTL_DAYS = 366`).
- The embedded email is just a label for tracking who holds which key.
- Use a dedicated beta secret, never your production `FINOPS_LICENSE_SECRET`.
  Anyone holding the secret can mint keys, so keep it to the trusted beta group.
- Free tier (~90% of features) and a 7-day Pro trial work with no key at all, so
  most testing needs nothing handed out.

## The proper fix (later)

Symmetric HMAC means the verifying secret must live on the client, which is
forgeable. The secure version is asymmetric: sign keys with a private key you
keep, ship only the public verify key in the package. Then testers enter only a
key (no secret) and nobody can forge. Flagged in the security audit.
