#!/usr/bin/env python3
"""
renew_certs.py - check each site's live SSL certificate and renew it with
acme.sh (domain + wildcard *.domain) when it is close to expiry or invalid.

Designed for cPanel shared hosting (e.g. GoDaddy Web Hosting Deluxe):
  * runs as your normal cPanel user, no root needed
  * issues certs with DNS validation only (Cloudflare by default; required for wildcards)
  * installs them on every matching cPanel site via acme.sh's cpanel_uapi hook
  * deletes expired certificates from cPanel's certificate storage

Usage:
  renew_certs.py [--config FILE] [--check-only] [--force] [--domain NAME] [--verbose]

Exit code is 0 if everything is fine, 1 if any domain failed (so cron can mail you).
Requires Python 3.6+ and openssl on the PATH.
"""

import argparse
import datetime
import fcntl
import json
import os
import socket
import ssl
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "certs.json")

DEFAULTS = {
    "renew_days_before": 14,
    "acme_sh": "~/.acme.sh/acme.sh",
    "acme_home": "~/.acme.sh",
    "ca_server": "letsencrypt",
    "email": "",
    "key_length": "2048",
    "dns_provider": "dns_cf",
    "dns_env": {},
    "dns_sleep": 0,
    "log_file": "",
    "connect_timeout": 10,
    "origin_ip": "",
    "delete_expired_certs": True,
    "uapi": "uapi",
}

LOG_FH = None
VERBOSE = False


# ---------------------------------------------------------------- logging ---

def log(msg, level="INFO"):
    line = "%s [%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    print(line, flush=True)
    if LOG_FH:
        LOG_FH.write(line + "\n")
        LOG_FH.flush()


def debug(msg):
    if VERBOSE:
        log(msg, "DEBUG")


# ----------------------------------------------------------------- config ---

def load_config(path):
    with open(path) as fh:
        try:
            cfg = json.load(fh)
        except ValueError as e:
            sys.exit("Config file %s is not valid JSON: %s" % (path, e))

    settings = dict(DEFAULTS)
    settings.update(cfg.get("settings", {}))
    for key in ("acme_sh", "acme_home", "log_file"):
        if settings[key]:
            settings[key] = os.path.expanduser(settings[key])

    domains = []
    for entry in cfg.get("domains", []):
        if isinstance(entry, str):
            entry = {"domain": entry}
        if not entry.get("domain"):
            sys.exit("Every entry in 'domains' needs a \"domain\" value: %r" % (entry,))
        d = entry["domain"].strip().lower()
        if d.startswith("*."):
            d = d[2:]
        entry["domain"] = d
        entry.setdefault("enabled", True)
        entry.setdefault("check_hosts", [d, "www." + d])
        domains.append(entry)

    if not domains:
        sys.exit("No domains listed in %s" % path)
    return settings, domains


# ------------------------------------------------------- live cert checks ---

def live_expiry(host, timeout, origin_ip=""):
    """Return the notAfter epoch of the cert served for host (no validation), or None."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((origin_ip or host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (ssl.SSLError, OSError):
        return None
    pem = ssl.DER_cert_to_PEM_cert(der)
    out = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
    return openssl_enddate(out)


def openssl_enddate(text):
    """Parse 'notAfter=Feb 12 04:38:09 2027 GMT' into an epoch, or None."""
    if "notAfter=" not in text:
        return None
    try:
        return ssl.cert_time_to_seconds(text.split("notAfter=", 1)[1].strip())
    except ValueError:
        return None


def check_live_cert(host, timeout, origin_ip=""):
    """Return (days_left or None, error string or None) for the cert served on host:443.

    If origin_ip is set, connect to that IP (your hosting server) instead of whatever
    DNS returns, so a Cloudflare-proxied site is checked at the origin, not the edge.
    """
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((origin_ip or host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLError as e:
        # covers expired, self-signed, hostname mismatch, etc.
        return None, "invalid certificate: %s" % (getattr(e, "verify_message", None) or e.reason or e)
    except ssl.CertificateError as e:
        return None, "invalid certificate: %s" % e
    except (socket.timeout, OSError) as e:
        return None, "could not connect: %s" % e

    expires = ssl.cert_time_to_seconds(cert["notAfter"])
    return (expires - time.time()) / 86400.0, None


# ------------------------------------------------------ local acme.sh cert ---

def is_ecc(settings):
    return str(settings["key_length"]).lower().startswith("ec")


def local_cert_path(settings, domain):
    folder = domain + ("_ecc" if is_ecc(settings) else "")
    return os.path.join(settings["acme_home"], folder, domain + ".cer")


def local_cert_usable(settings, domain, min_days):
    """True if acme.sh already holds a cert for domain + *.domain valid for > min_days."""
    path = local_cert_path(settings, domain)
    if not os.path.isfile(path):
        return False
    # -checkend returns 0 when the cert will NOT expire within the given seconds
    rc = subprocess.call(["openssl", "x509", "-noout", "-checkend", str(int(min_days * 86400)), "-in", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if rc != 0:
        return False
    out = subprocess.run(["openssl", "x509", "-noout", "-text", "-in", path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
    return ("DNS:*." + domain) in out and ("DNS:" + domain) in out


# ---------------------------------------------------------------- acme.sh ---

def run_acme(settings, args, env):
    cmd = [settings["acme_sh"], "--home", settings["acme_home"]] + args
    debug("running: " + " ".join(cmd))
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    for line in proc.stdout.splitlines():
        debug("  acme.sh: " + line)
    return proc.returncode, proc.stdout


def build_env(settings, entry):
    env = dict(os.environ)
    env.update({k: str(v) for k, v in settings.get("dns_env", {}).items()})
    env.update({k: str(v) for k, v in entry.get("dns_env", {}).items()})
    # Install on every cPanel site that the cert matches (domain + all subdomain sites).
    env["DEPLOY_CPANEL_AUTO_ENABLED"] = "true"
    env["DEPLOY_CPANEL_AUTO_NOMATCH"] = "false"
    return env


def issue(settings, entry, env):
    d = entry["domain"]
    args = ["--issue", "--force",
            "-d", d, "-d", "*." + d,
            "--dns", entry.get("dns_provider", settings["dns_provider"]),
            "--server", settings["ca_server"],
            "--keylength", str(settings["key_length"])]
    sleep = entry.get("dns_sleep", settings["dns_sleep"])
    if sleep:
        args += ["--dnssleep", str(sleep)]
    rc, out = run_acme(settings, args, env)
    if rc != 0:
        tail = "\n".join(out.strip().splitlines()[-8:])
        log("%s: certificate issue FAILED (acme.sh exit %d):\n%s" % (d, rc, tail), "ERROR")
        return False
    log("%s: new certificate issued for %s and *.%s" % (d, d, d))
    return True


def deploy(settings, entry, env):
    d = entry["domain"]
    args = ["--deploy", "-d", d, "--deploy-hook", "cpanel_uapi"]
    if is_ecc(settings):
        args.append("--ecc")
    rc, out = run_acme(settings, args, env)
    if rc != 0:
        tail = "\n".join(out.strip().splitlines()[-8:])
        log("%s: deploy to cPanel FAILED (acme.sh exit %d):\n%s" % (d, rc, tail), "ERROR")
        return False
    log("%s: cPanel accepted the certificate" % d)
    return verify_served(settings, entry)


def verify_served(settings, entry):
    """Confirm each check_host now serves the certificate acme.sh holds."""
    d = entry["domain"]
    path = local_cert_path(settings, d)
    out = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", path],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
    want = openssl_enddate(out)
    if want is None:
        log("%s: could not read new certificate at %s to verify it" % (d, path), "ERROR")
        return False
    origin = entry.get("origin_ip", settings["origin_ip"])
    bad = []
    for host in entry["check_hosts"]:
        got = None
        for attempt in range(6):          # give Apache up to ~30s to reload
            got = live_expiry(host, settings["connect_timeout"], origin)
            if got is not None and abs(got - want) < 120:
                break
            time.sleep(5)
        if got is not None and abs(got - want) < 120:
            log("%s: verified - now serving the new certificate" % host)
        else:
            seen = datetime.datetime.fromtimestamp(got).strftime("%Y-%m-%d") if got else "nothing"
            log("%s: still serving a certificate expiring %s, expected %s - install did not take effect"
                % (host, seen, datetime.datetime.fromtimestamp(want).strftime("%Y-%m-%d")), "ERROR")
            bad.append(host)
    return not bad


# ------------------------------------------------- expired cert cleanup ---

def uapi_call(settings, func, *params):
    """Run `uapi SSL <func>` and return its data list/dict, or raise RuntimeError."""
    cmd = [settings["uapi"], "--output=json", "SSL", func] + list(params)
    debug("running: " + " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        resp = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError("uapi SSL %s returned no JSON (exit %d): %s"
                           % (func, proc.returncode, (proc.stderr or proc.stdout).strip()[:300]))
    result = resp.get("result", resp)
    if not result.get("status"):
        raise RuntimeError("uapi SSL %s failed: %s" % (func, result.get("errors")))
    return result.get("data")


def installed_cert_ids(settings):
    """IDs of certificates currently installed on a site - never delete these."""
    ids = set()
    for host in uapi_call(settings, "installed_hosts") or []:
        cert = host.get("certificate") or {}
        if cert.get("id"):
            ids.add(cert["id"])
    return ids


def cleanup_expired(settings, args):
    """Delete expired certificates from cPanel storage. Returns True on success."""
    try:
        certs = uapi_call(settings, "list_certs") or []
        in_use = installed_cert_ids(settings)
    except (RuntimeError, OSError) as e:
        log("cleanup: could not list cPanel certificates: %s" % e, "ERROR")
        return False

    now = time.time()
    ok = True
    deleted = 0
    for cert in certs:
        cid = cert.get("id")
        try:
            not_after = float(cert.get("not_after"))
        except (TypeError, ValueError):
            continue
        if not cid or not_after > now:
            continue
        names = ", ".join(cert.get("domains") or []) or cid
        expired_on = datetime.datetime.fromtimestamp(not_after).strftime("%Y-%m-%d")
        if cid in in_use:
            log("cleanup: %s (expired %s) is still installed on a site - leaving it" % (names, expired_on), "WARN")
            continue
        if args.check_only:
            log("cleanup: would delete expired certificate %s (expired %s)" % (names, expired_on))
            continue
        try:
            uapi_call(settings, "delete_cert", "id=" + cid)
            log("cleanup: deleted expired certificate %s (expired %s)" % (names, expired_on))
            deleted += 1
        except RuntimeError as e:
            log("cleanup: could not delete %s: %s" % (names, e), "ERROR")
            ok = False
    if deleted == 0 and ok:
        debug("cleanup: no expired certificates to delete")
    return ok


# ------------------------------------------------------------------- main ---

def process(settings, entry, args):
    d = entry["domain"]
    renew_days = float(entry.get("renew_days_before", settings["renew_days_before"]))

    # 1. Check what browsers actually see
    needs_renewal = args.force
    unreachable = []
    for host in entry["check_hosts"]:
        days, err = check_live_cert(host, settings["connect_timeout"],
                                    entry.get("origin_ip", settings["origin_ip"]))
        if err and err.startswith("could not connect"):
            # Site down / DNS problem: a new cert won't help, so don't burn rate limits.
            log("%s: %s" % (host, err), "WARN")
            unreachable.append(host)
        elif err:
            log("%s: %s" % (host, err), "WARN")
            needs_renewal = True
        elif days <= renew_days:
            log("%s: expires in %.1f days (threshold %g) - renewal needed" % (host, days, renew_days))
            needs_renewal = True
        else:
            log("%s: OK, expires in %.1f days" % (host, days))

    if not needs_renewal:
        if unreachable:
            log("%s: could not check %s - not renewing" % (d, ", ".join(unreachable)), "ERROR")
            return False
        return True
    if args.check_only:
        log("%s: would renew (check-only mode, nothing changed)" % d)
        return True

    env = build_env(settings, entry)

    # 2. If acme.sh already has a fresh cert (e.g. last deploy failed), just redeploy it.
    #    Avoids hitting Let's Encrypt rate limits by re-issuing on every cron run.
    if not args.force and local_cert_usable(settings, d, renew_days):
        log("%s: acme.sh already holds a fresh certificate - redeploying it" % d)
    else:
        if not issue(settings, entry, env):
            return False

    return deploy(settings, entry, env)


def main():
    global LOG_FH, VERBOSE
    p = argparse.ArgumentParser(description="Check and renew wildcard SSL certs via acme.sh")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="path to JSON config (default: certs.json next to this script)")
    p.add_argument("--check-only", action="store_true", help="report status only, never renew")
    p.add_argument("--force", action="store_true", help="renew even if not close to expiry")
    p.add_argument("--domain", help="only process this domain")
    p.add_argument("--verbose", "-v", action="store_true", help="show acme.sh output")
    args = p.parse_args()
    VERBOSE = args.verbose

    settings, domains = load_config(args.config)
    if settings["log_file"]:
        LOG_FH = open(settings["log_file"], "a")

    if not args.check_only and not os.access(settings["acme_sh"], os.X_OK):
        log("acme.sh not found or not executable at %s" % settings["acme_sh"], "ERROR")
        return 1

    # Prevent two cron runs overlapping
    lock = open(os.path.join(settings["acme_home"] if os.path.isdir(settings["acme_home"]) else SCRIPT_DIR,
                             ".renew_certs.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run is already in progress, exiting", "WARN")
        return 0

    if args.domain:
        domains = [e for e in domains if e["domain"] == args.domain.lower()]
        if not domains:
            log("domain %s is not in the config" % args.domain, "ERROR")
            return 1

    failed = []
    for entry in domains:
        if not entry["enabled"]:
            debug("%s: disabled in config, skipping" % entry["domain"])
            continue
        try:
            if not process(settings, entry, args):
                failed.append(entry["domain"])
        except Exception as e:  # keep going with the other domains
            log("%s: unexpected error: %s" % (entry["domain"], e), "ERROR")
            failed.append(entry["domain"])

    # Clean up after renewals so freshly replaced certs are no longer "installed".
    if settings["delete_expired_certs"] and not args.domain:
        if not cleanup_expired(settings, args):
            failed.append("expired-cert cleanup")

    if failed:
        log("FAILED: " + ", ".join(failed), "ERROR")
        return 1
    log("done, all domains OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
