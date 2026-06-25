"""Free email-provider (ESP) detection via DNS MX records. No API, no cost.

Looks up the email domain's MX hosts and maps them to a provider:
  Microsoft 365  -> hosts contain outlook / office365 / *.protection.outlook.com
  Google Workspace -> hosts contain google / googlemail / aspmx
  Other -> resolves but isn't MS/Google
  Unknown -> no MX / can't resolve

Results are cached per domain (many leads share a domain), so a big list only does
one lookup per unique domain.
"""
import threading

try:
    import dns.resolver
    _HAVE_DNS = True
except Exception:  # dnspython not installed
    _HAVE_DNS = False

_cache = {}
_lock = threading.Lock()


def _mx_hosts(domain):
    """Space-joined lowercase MX hostnames for a domain, or None if unresolvable.
    Tries the system resolver first, then public resolvers (8.8.8.8 / 1.1.1.1) in
    case the container has no resolver configured."""
    if not _HAVE_DNS:
        return None
    for configure in (True, False):
        try:
            r = dns.resolver.Resolver(configure=configure)
            if not configure:
                r.nameservers = ["8.8.8.8", "1.1.1.1"]
            r.timeout = 5
            r.lifetime = 5
            ans = r.resolve(domain, "MX")
            return " ".join(str(rec.exchange).lower() for rec in ans)
        except Exception:
            continue
    return None


def detect(email):
    """Return Microsoft | Google | Other | Unknown for an email address."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return "Unknown"
    domain = email.rsplit("@", 1)[-1].strip()
    if not domain:
        return "Unknown"
    with _lock:
        if domain in _cache:
            return _cache[domain]
    hosts = _mx_hosts(domain)
    if hosts is None:
        label = "Unknown"
    elif any(x in hosts for x in ("outlook", "office365", "microsoft", "protection.outlook")):
        label = "Microsoft"
    elif any(x in hosts for x in ("google", "googlemail", "aspmx")):
        label = "Google"
    else:
        label = "Other"
    with _lock:
        _cache[domain] = label
    return label
