# renew_certs

Wildcard Let's Encrypt certificates for cPanel shared hosting, renewed automatically from cron.

For each domain in a JSON config, `renew_certs.py`:

1. Checks the certificate each site is actually serving (optionally on the origin server, behind Cloudflare's proxy).
2. When it is invalid or expiring within `renew_days_before` days, issues a new certificate for `domain` + `*.domain` with [acme.sh](https://github.com/acmesh-official/acme.sh) using DNS validation.
3. Installs it on every matching cPanel site via acme.sh's `cpanel_uapi` hook.
4. Verifies each site is now serving the new certificate.
5. Deletes expired certificates from cPanel's certificate storage (never ones still installed on a site).

It runs as your normal cPanel user (no root), and exits non-zero on any failure so cron can email you. Built for GoDaddy Web Hosting Deluxe with DNS on Cloudflare, but any cPanel host and any [acme.sh DNS provider](https://github.com/acmesh-official/acme.sh/wiki/dnsapi) should work.

## Requirements

- cPanel hosting with SSH or Terminal access, and the `uapi` command
- Python 3.6+ and `openssl` (both normally present on cPanel hosts)
- acme.sh installed in your home directory
- DNS hosted with a provider acme.sh supports (Cloudflare by default)

## Setup

1. **Create a Cloudflare API token** (My Profile → API Tokens → Create Token → "Edit zone DNS" template) with *Zone → DNS → Edit* and *Zone → Zone → Read*, Zone Resources: *All zones*.

2. **Copy the files** to a folder outside `public_html`, e.g. `~/ssl/`:
   ```sh
   cp certs.example.json certs.json
   chmod 700 renew_certs.py
   chmod 600 certs.json
   mkdir -p ~/logs
   ```

3. **Edit `certs.json`**: add your token, your domains, and `origin_ip` (see below).

4. **Test without changing anything:**
   ```sh
   python3 ~/ssl/renew_certs.py --check-only
   ```

5. **Do one real renewal:**
   ```sh
   python3 ~/ssl/renew_certs.py --domain example.com --force -v
   ```

6. **Add a daily cron job** (cPanel → Cron Jobs; set "Cron Email" to your address):
   ```
   17 3 * * * /usr/bin/python3 $HOME/ssl/renew_certs.py >/dev/null
   ```
   You only get mail when something fails; the full history goes to the log file.

If acme.sh's own renewal cron (`acme.sh --cron`) is installed, remove it with `acme.sh --uninstall-cronjob` so the two don't compete.

## Config

| Setting | Default | Meaning |
|---|---|---|
| `renew_days_before` | `14` | Renew when the served certificate has this many days left or fewer |
| `acme_sh` | `~/.acme.sh/acme.sh` | Path to acme.sh |
| `ca_server` | `letsencrypt` | acme.sh CA (`letsencrypt`, `zerossl`, ...) |
| `key_length` | `2048` | `2048`/`4096` for RSA, `ec-256`/`ec-384` for ECDSA |
| `dns_provider` | `dns_cf` | acme.sh DNS hook |
| `dns_env` | `{}` | Credentials for the DNS hook, e.g. `CF_Token` |
| `dns_sleep` | `0` | Seconds to wait for DNS propagation (0 = acme.sh default) |
| `origin_ip` | `""` | Check certificates on this IP instead of what DNS returns |
| `delete_expired_certs` | `true` | Remove expired certificates from cPanel storage |
| `log_file` | `""` | Append log lines here |
| `connect_timeout` | `10` | Seconds per TLS check |

Each entry in `domains` needs `domain`, and can override `check_hosts` (default: the domain and `www.`), `enabled`, `renew_days_before`, `dns_provider`, `dns_env`, `dns_sleep` and `origin_ip`.

### Sites behind Cloudflare's proxy

Visitors to a proxied site see Cloudflare's own certificate, not yours. Your certificate secures the connection from Cloudflare to your server, and with SSL mode **Full (strict)** Cloudflare refuses to connect if it is invalid. Set `origin_ip` to your hosting server's IP (cPanel shows it as "Shared IP Address") so the script checks the certificate on your server rather than Cloudflare's. Use `"origin_ip": ""` on a domain that isn't hosted there.

## Options

```
--config FILE   config path (default: certs.json next to the script)
--check-only    report status and what would change; change nothing
--force         renew even if not close to expiry
--domain NAME   only this domain (skips expired-cert cleanup)
-v, --verbose   show acme.sh and uapi output
```

## Notes

- `*.example.com` covers `shop.example.com` but not `a.b.example.com`.
- If a site can't be reached, the script reports an error but doesn't request a certificate, to avoid Let's Encrypt rate limits. If acme.sh already holds a fresh certificate (e.g. the last install failed), it reinstalls that instead of requesting another.
- Let's Encrypt is shortening certificate lifetimes (64 days from February 2027, 45 days from February 2028). The script renews based on the real expiry date, so it keeps working; renewals just happen more often.
- On GoDaddy, a `hooks2 ... install_ssl: exit 255` warning during install comes from GoDaddy's own post-install hook and is harmless. The verification step confirms whether the install took effect.
