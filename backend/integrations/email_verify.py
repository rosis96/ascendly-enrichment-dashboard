"""Free email verification — no API, no cost.

Runs the layers a paid verifier does that DON'T need to touch the mail server:
  1. Syntax        — is it a valid address shape? (RFC-ish)
  2. MX / DNS       — does the domain have a mail server? (no MX = cannot receive mail)
  3. Disposable     — is it a throwaway domain (mailinator, tempmail, ...)?
  4. Role account   — is the local part a role (info@, sales@) rather than a person?

What this CANNOT do (and why Reoon still runs after it):
  - Confirm a specific mailbox exists on a valid domain. That needs an SMTP
    handshake, which is unreliable/blocked from cloud IPs and can hurt sender
    reputation. Reoon handles that layer.

Design goal: SAFE rejections only. We reject an email as "invalid" ONLY when it
is definitively undeliverable (bad syntax, no MX, disposable). We NEVER reject a
deliverable email — a domain with no mail server truly cannot receive email. On
any DNS uncertainty (timeout / resolver failure) we FAIL OPEN (verdict "ok") so
the email falls through to Reoon rather than being wrongly dropped.

MX results are cached per domain, so a big list only does one lookup per unique
domain. Returns a small dict the pipeline can read.
"""
import re
import threading

try:
    import dns.resolver
    _HAVE_DNS = True
except Exception:  # dnspython not installed
    _HAVE_DNS = False

# Valid overall shape. Intentionally conservative (won't reject unusual-but-real
# addresses); the real "does the mailbox exist" check is Reoon's job.
_SYNTAX = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

# Known throwaway / temporary-inbox domains. Not exhaustive, but catches the
# common ones seen in scraped lists.
_DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamailblock.com", "sharklasers.com",
    "10minutemail.com", "10minutemail.net", "tempmail.com", "temp-mail.org", "tempmailo.com",
    "throwawaymail.com", "yopmail.com", "yopmail.fr", "getnada.com", "nada.email",
    "trashmail.com", "trashmail.net", "mailnesia.com", "maildrop.cc", "dispostable.com",
    "fakeinbox.com", "fakemailgenerator.com", "mohmal.com", "emailondeck.com", "mintemail.com",
    "spamgourmet.com", "mytemp.email", "moakt.com", "burnermail.io", "tempinbox.com",
    "discard.email", "spam4.me", "grr.la", "mailcatch.com", "inboxkitten.com", "tmail.ws",
    "20minutemail.com", "33mail.com", "anonbox.net", "correotemporal.org", "temp-mail.io",
}

# Role-based local parts — valid mailboxes, but usually not a specific person.
# We FLAG these (role=True) rather than reject; the pipeline can decide.
_ROLE = {
    "info", "sales", "support", "admin", "administrator", "contact", "hello", "hi",
    "team", "office", "billing", "accounts", "accounting", "hr", "jobs", "careers",
    "marketing", "press", "media", "help", "service", "services", "enquiries", "inquiries",
    "noreply", "no-reply", "donotreply", "do-not-reply", "webmaster", "postmaster",
    "abuse", "privacy", "legal", "security", "orders", "order", "feedback", "general",
}

_cache = {}
_lock = threading.Lock()


def _has_mx(domain):
    """True if the domain can receive mail (has MX, or falls back to an A record).
    Returns None when we genuinely can't tell (no resolver / total DNS failure) so
    the caller can fail open. Cached per domain."""
    with _lock:
        if domain in _cache:
            return _cache[domain]

    result = None
    if _HAVE_DNS:
        got_answer = False
        # Try MX first (system resolver, then public resolvers).
        for configure in (True, False):
            try:
                r = dns.resolver.Resolver(configure=configure)
                if not configure:
                    r.nameservers = ["8.8.8.8", "1.1.1.1"]
                r.timeout = 5
                r.lifetime = 5
                ans = r.resolve(domain, "MX")
                if len(ans) > 0:
                    result = True
                    got_answer = True
                    break
            except dns.resolver.NoAnswer:
                got_answer = True  # domain exists, just no MX -> check A next
            except dns.resolver.NXDOMAIN:
                result = False      # domain does not exist -> definitively invalid
                got_answer = True
                break
            except Exception:
                continue            # timeout / servfail -> try next / fail open
        # No MX but domain resolves: some hosts accept mail on the A record.
        if result is None and got_answer:
            for configure in (True, False):
                try:
                    r = dns.resolver.Resolver(configure=configure)
                    if not configure:
                        r.nameservers = ["8.8.8.8", "1.1.1.1"]
                    r.timeout = 5
                    r.lifetime = 5
                    ans = r.resolve(domain, "A")
                    result = len(ans) > 0
                    break
                except dns.resolver.NXDOMAIN:
                    result = False
                    break
                except dns.resolver.NoAnswer:
                    result = False
                    break
                except Exception:
                    continue
    # else: no dnspython -> result stays None (fail open)

    with _lock:
        _cache[domain] = result
    return result


def check(email):
    """Free verification verdict for an email.

    Returns dict:
      verdict : "invalid" (definitively undeliverable — reject) | "ok" (passes free
                checks; still send to Reoon to confirm the mailbox)
      reason  : short human string
      label   : short status label ("invalid", "ok", "role")
      role    : bool, True if a role account (info@, sales@, ...)
    """
    e = (email or "").strip().lower()
    if not e or "@" not in e or not _SYNTAX.match(e):
        return {"verdict": "invalid", "reason": "bad syntax", "label": "invalid", "role": False}

    local, domain = e.rsplit("@", 1)
    if domain in _DISPOSABLE:
        return {"verdict": "invalid", "reason": "disposable domain", "label": "invalid", "role": False}

    mx = _has_mx(domain)
    if mx is False:
        return {"verdict": "invalid", "reason": "no mail server (MX)", "label": "invalid", "role": False}
    # mx is None -> DNS uncertain -> fail open (let Reoon decide).

    role = local in _ROLE or local.split("+", 1)[0] in _ROLE
    return {
        "verdict": "ok",
        "reason": "role account" if role else "passes free checks",
        "label": "role" if role else "ok",
        "role": role,
    }
