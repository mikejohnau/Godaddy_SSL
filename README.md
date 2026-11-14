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
| `dns_provider` | `dns_cf` | acme.sh DNS hook — see [DNS providers](#dns-providers) |
| `dns_env` | `{}` | Credentials for the DNS hook, e.g. `CF_Token` — see [DNS providers](#dns-providers) |
| `dns_sleep` | `0` | Seconds to wait for DNS propagation (0 = acme.sh default) |
| `origin_ip` | `""` | Check certificates on this IP instead of what DNS returns |
| `delete_expired_certs` | `true` | Remove expired certificates from cPanel storage |
| `log_file` | `""` | Append log lines here |
| `connect_timeout` | `10` | Seconds per TLS check |

Each entry in `domains` needs `domain`, and can override `check_hosts` (default: the domain and `www.`), `enabled`, `renew_days_before`, `dns_provider`, `dns_env`, `dns_sleep` and `origin_ip`.

## DNS providers

Wildcard certificates need DNS validation: acme.sh publishes a temporary `_acme-challenge` TXT record through your DNS provider's API. Two settings choose how:

- `dns_provider`: the name of the acme.sh DNS hook for your provider.
- `dns_env`: that hook's credentials, as a map of the variable names it reads.

acme.sh ships [205 DNS hooks](https://github.com/acmesh-official/acme.sh/tree/master/dnsapi). Here are some common ones, with the variables taken from each hook's source:

| Provider | `dns_provider` | `dns_env` keys | Notes |
|---|---|---|---|
| Cloudflare | `dns_cf` | `CF_Token` | Token with *Zone → DNS → Edit* and *Zone → Zone → Read* on all zones. `CF_Account_ID` / `CF_Zone_ID` optional. Older global-key setups use `CF_Key` + `CF_Email` instead |
| GoDaddy DNS | `dns_gd` | `GD_Key`, `GD_Secret` | GoDaddy limits production API access by account size; check yours works before relying on it |
| cPanel Zone Editor | `dns_cpanel` | `cPanel_Username`, `cPanel_Apitoken`, `cPanel_Hostname` | For DNS hosted in cPanel itself. `cPanel_Hostname` is like `https://hostname:2083`; create the token in cPanel → Manage API Tokens |
| AWS Route 53 | `dns_aws` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | |
| Azure DNS | `dns_azure` | `AZUREDNS_SUBSCRIPTIONID`, `AZUREDNS_TENANTID`, `AZUREDNS_APPID`, `AZUREDNS_CLIENTSECRET` | Service principal credentials |
| Bunny DNS | `dns_bunny` | `BUNNY_API_KEY` | |
| deSEC | `dns_desec` | `DEDYN_TOKEN` | |
| DigitalOcean | `dns_dgon` | `DO_API_KEY` | |
| DuckDNS | `dns_duckdns` | `DuckDNS_Token` | |
| Dynu | `dns_dynu` | `Dynu_ClientId`, `Dynu_Secret` | |
| Gandi LiveDNS | `dns_gandi_livedns` | `GANDI_LIVEDNS_KEY` | |
| Hetzner Cloud DNS | `dns_hetznercloud` | `HETZNER_TOKEN` | |
| Hurricane Electric | `dns_he` | `HE_Username`, `HE_Password` | |
| IONOS | `dns_ionos` | `IONOS_PREFIX`, `IONOS_SECRET` | |
| Linode / Akamai | `dns_linode_v4` | `LINODE_V4_API_KEY` | |
| Name.com | `dns_namecom` | `Namecom_Username`, `Namecom_Token` | |
| Namecheap | `dns_namecheap` | `NAMECHEAP_USERNAME`, `NAMECHEAP_API_KEY`, `NAMECHEAP_SOURCEIP` | Whitelist your server's IP in Namecheap. The hook rewrites all of a domain's records, so keep a backup |
| NameSilo | `dns_namesilo` | `Namesilo_Key` | |
| Netlify | `dns_netlify` | `NETLIFY_ACCESS_TOKEN` | |
| OVHcloud | `dns_ovh` | `OVH_AK`, `OVH_AS`, `OVH_CK`, `OVH_END_POINT` | `OVH_END_POINT` defaults to `ovh-eu` |
| Porkbun | `dns_porkbun` | `PORKBUN_API_KEY`, `PORKBUN_SECRET_API_KEY` | Turn on API access for each domain in Porkbun |
| Vultr | `dns_vultr` | `VULTR_API_KEY` | |

For any other provider, find its file in acme.sh's [`dnsapi` folder](https://github.com/acmesh-official/acme.sh/tree/master/dnsapi). The variables it needs are listed under `Options:` at the top of the file, and the [dnsapi wiki](https://github.com/acmesh-official/acme.sh/wiki/dnsapi) has setup notes for most providers.

**One provider for everything** goes in `settings`:

```json
"settings": {
  "dns_provider": "dns_porkbun",
  "dns_env": { "PORKBUN_API_KEY": "pk1_...", "PORKBUN_SECRET_API_KEY": "sk1_..." }
}
```

**A different provider or account for one domain** goes in that domain's entry, which overrides `settings`:

```json
"domains": [
  { "domain": "example.com" },
  { "domain": "example.org", "dns_provider": "dns_namecheap",
    "dns_env": { "NAMECHEAP_USERNAME": "me", "NAMECHEAP_API_KEY": "...", "NAMECHEAP_SOURCEIP": "203.0.113.10" } },
  { "domain": "example.net", "dns_env": { "CF_Token": "token-from-a-second-cloudflare-account" } }
]
```

A domain's `dns_env` is added on top of the one in `settings`, so give each key its full value there. If DNS changes are slow to appear at your provider and validation fails, set `dns_sleep` (seconds) to make acme.sh wait longer before asking Let's Encrypt to check. After the first successful run, acme.sh also saves the credentials in `~/.acme.sh/account.conf`.

## Sites behind Cloudflare's proxy

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
