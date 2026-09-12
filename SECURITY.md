# Security

## Threat model, stated plainly

3DPrintTally is a **single-operator tool with no user accounts.** There is no
login page, no roles, and no per-user data separation. Optional HTTP Basic
authentication is one shared credential for the whole application.

The assumption is that the app runs on a machine you control, reached over
loopback, a LAN you trust, a VPN, or behind a reverse proxy that performs the
real access control.

**Anyone who can reach the port can read and modify everything** — your catalog,
your costs, your filament purchases and your customer job history.

Because of that:

- `BIND_ADDRESS` defaults to `127.0.0.1`.
- Authentication is off by default, because shipping a default credential is
  worse than shipping none.
- Setting only one of `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD_HASH` is a
  startup error rather than a silent no-auth fallback.

Do not put this on the public internet without authentication in front of it.

## What is implemented

- CSRF token on every mutating request, compared with `hmac.compare_digest`
- Origin header check on mutating requests
- Content Security Policy with a per-request nonce; `object-src`, `base-uri`
  and `frame-ancestors` locked down
- `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and
  `Cache-Control: no-store, private` on non-static responses
- Request caps: 16 MB body, 512 KB form memory, 2000 form parts, 3000 labels
- Uploaded images re-encoded from validated pixel data, which drops trailing
  executable payloads
- Session cookies `HttpOnly` + `SameSite=Lax`; `Secure` via `COOKIE_SECURE`
- A file lock serializing writes, so concurrent requests cannot interleave
- Containers run read-only, non-root, `cap_drop: ALL`, `no-new-privileges`,
  with `/tmp` on `noexec,nosuid` tmpfs
- Session key generated once at `data/.session-key` with mode 600; no shipped
  default secret
- `/healthz` is unauthenticated by design and exposes only a liveness flag

## What is not

- No rate limiting or brute-force lockout on Basic auth
- No audit log of who changed what (there is no "who")
- No encryption at rest; the SQLite file is readable by anything with filesystem
  access
- No 2FA, no session revocation, no password rotation policy
- HTTPS and HSTS are entirely the reverse proxy's responsibility

## Dependencies

```sh
python tools/check_dependencies.py
```

Queries OSV with package names and versions only. Dependencies are pinned in
`requirements.txt`.

## Reporting a vulnerability

Open a private security advisory through the repository's Security tab rather
than a public issue. Include a reproduction and the version or commit.

This is a small project maintained by one person alongside a day job. Expect a
best-effort response, not an SLA.
