"""Credential prompting, verification ("login"), and storage for provider admin APIs.

Resolution order per provider: environment variable -> saved credential -> interactive prompt.
Every credential is verified with a live read-only call before use; a rejected saved key
triggers a re-prompt. Saved credentials go to the OS secret store (macOS Keychain, Windows
Credential Manager, Linux Secret Service) under the service name "frontier-usage".
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = "frontier-usage"
MAX_ATTEMPTS = 3

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic API",
        "env": "ANTHROPIC_ADMIN_KEY",
        "prefix": "sk-ant-admin",
        "where": "https://console.anthropic.com/settings/admin-keys",
        "headers": lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"},
        "verify": "https://api.anthropic.com/v1/organizations/api_keys",  # needs full admin scope, unlike /users
    },
    "claude_enterprise": {
        # claude.ai chat, Cowork, Claude Code etc. on an Enterprise plan. Key from claude.ai >
        # Organization settings > API with the read:analytics scope; a Console Admin key can't call it.
        "label": "Claude Enterprise",
        "env": "CLAUDE_ENTERPRISE_KEY",
        "prefix": "sk-ant-api",
        "where": "https://claude.ai/admin-settings/api-access",
        "headers": lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"},
        "verify": "https://api.anthropic.com/v1/organizations/analytics/user_usage_report",
        "verify_params": lambda: [("starting_at", _analytics_probe_start()), ("limit", "1")],
    },
    "openai": {
        "label": "OpenAI",
        "env": "OPENAI_ADMIN_KEY",
        "prefix": "sk-admin-",
        "where": "https://platform.openai.com/settings/organization/admin-keys",
        "headers": lambda k: {"Authorization": f"Bearer {k}"},
        "verify": "https://api.openai.com/v1/organization/users",
    },
}


def _analytics_probe_start() -> str:
    """Yesterday at midnight UTC; the Analytics API rejects dates before 2026-01-01."""
    from datetime import date, datetime, timedelta, timezone
    day = max(datetime.now(timezone.utc).date() - timedelta(days=1), date(2026, 1, 1))
    return f"{day.isoformat()}T00:00:00Z"


class ApiError(Exception):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} from {url}: {body}")
        self.status = status
        self.body = body

    @property
    def api_message(self) -> str:
        """The provider's own error text, e.g. "API key is invalid."."""
        try:
            err = json.loads(self.body).get("error", {})
            return (err.get("message") if isinstance(err, dict) else str(err)) or self.body[:200]
        except (ValueError, AttributeError):
            return self.body[:200]


def _ssl_context() -> ssl.SSLContext:
    # python.org's macOS builds ship without a CA bundle; use certifi's when it's installed.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


def http_request(method: str, url: str, headers: dict[str, str], body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {**headers, "User-Agent": "frontier-usage/1.0"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL) as resp:
            raw = resp.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, url, e.read().decode(errors="replace")[:500]) from None


def http_get(url: str, headers: dict[str, str], params: list[tuple[str, str]] | None = None) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    try:
        return http_request("GET", full, headers)
    except ApiError as e:
        raise ApiError(e.status, url, e.body) from None


# ---------------------------------------------------------------- storage
#
# One backend per OS secret store, all stdlib: macOS Keychain (`security`), Windows Credential
# Manager (advapi32 via ctypes), Linux Secret Service (`secret-tool`, from libsecret-tools).
# The `keyring` package wins if installed. When no store works (e.g. a headless Linux box with
# no D-Bus session) credentials fall back to a per-user file readable only by the owner.


class _KeyringLib:
    name = "system keyring"

    def __init__(self):
        import keyring  # optional dependency
        self.k = keyring

    def get(self, account):
        return self.k.get_password(SERVICE, account)

    def set(self, account, secret):
        self.k.set_password(SERVICE, account, secret)

    def delete(self, account):
        try:
            self.k.delete_password(SERVICE, account)
            return True
        except Exception:
            return False


class _MacKeychain:
    name = "macOS Keychain"

    def get(self, account):
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True)
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    def set(self, account, secret):
        # Fed through `security -i` on stdin so the secret never appears in the process list.
        cmd = f'add-generic-password -U -s {SERVICE} -a {account} -l "{SERVICE} {account}" -w "{secret}"\n'
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
        if r.returncode != 0 or "error" in r.stderr.lower():
            raise RuntimeError(r.stderr.strip() or "security exited non-zero")

    def delete(self, account):
        r = subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                           capture_output=True, text=True)
        return r.returncode == 0


class _LinuxSecretTool:
    name = "Secret Service (secret-tool)"

    def _attrs(self, account):
        return ["service", SERVICE, "account", account]

    def get(self, account):
        r = subprocess.run(["secret-tool", "lookup", *self._attrs(account)], capture_output=True, text=True)
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    def set(self, account, secret):
        # secret-tool reads the secret from stdin.
        r = subprocess.run(["secret-tool", "store", f"--label={SERVICE} {account}", *self._attrs(account)],
                           input=secret, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "secret-tool exited non-zero")

    def delete(self, account):
        r = subprocess.run(["secret-tool", "clear", *self._attrs(account)], capture_output=True, text=True)
        return r.returncode == 0


class _WindowsCredMan:
    name = "Windows Credential Manager"
    GENERIC, PERSIST_LOCAL_MACHINE, ERROR_NOT_FOUND = 1, 2, 1168

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        self.ct, self.CREDENTIAL = ctypes, CREDENTIAL
        self.api = ctypes.WinDLL("advapi32", use_last_error=True)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    def _target(self, account):
        return f"{SERVICE}:{account}"

    def get(self, account):
        ct = self.ct
        pcred = ct.POINTER(self.CREDENTIAL)()
        if not self.api.CredReadW(self._target(account), self.GENERIC, 0, ct.byref(pcred)):
            return None
        try:
            c = pcred.contents
            return ct.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le") or None
        finally:
            self.api.CredFree(pcred)

    def set(self, account, secret):
        ct = self.ct
        blob = secret.encode("utf-16-le")
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self.CREDENTIAL(Type=self.GENERIC, TargetName=self._target(account), UserName=account,
                               CredentialBlobSize=len(blob), CredentialBlob=buf,
                               Persist=self.PERSIST_LOCAL_MACHINE)
        if not self.api.CredWriteW(ct.byref(cred), 0):
            raise OSError(ct.get_last_error(), "CredWriteW failed")

    def delete(self, account):
        return bool(self.api.CredDeleteW(self._target(account), self.GENERIC, 0))


def config_dir() -> Path:
    """Per-user settings folder: %APPDATA%\\frontier-usage or ~/.config/frontier-usage."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / SERVICE


def load_settings() -> dict:
    """Non-secret preferences (e.g. the Grafana URL). Secrets go through save()/load_saved()."""
    try:
        return json.loads((config_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(**changes) -> None:
    data = {**load_settings(), **changes}
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "settings.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


class _File:
    def __init__(self):
        self.path = config_dir() / "credentials.json"
        self.name = str(self.path)

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 on POSIX; on Windows the per-user %APPDATA% ACL is what protects it.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def get(self, account):
        return self._read().get(account)

    def set(self, account, secret):
        data = self._read()
        data[account] = secret
        self._write(data)

    def delete(self, account):
        data = self._read()
        if data.pop(account, None) is None:
            return False
        self._write(data)
        return True


def _native_store():
    try:
        return _KeyringLib()
    except ImportError:
        pass
    try:
        if sys.platform == "darwin" and shutil.which("security"):
            return _MacKeychain()
        if sys.platform == "win32":
            return _WindowsCredMan()
        if shutil.which("secret-tool"):
            return _LinuxSecretTool()
    except Exception:  # store unusable on this machine; the file fallback still works
        pass
    return None


_NATIVE = _native_store()
_FILE = _File()


def store_name() -> str:
    return _NATIVE.name if _NATIVE else _FILE.name


def _stores():
    return [s for s in (_NATIVE, _FILE) if s]


def load_saved(provider: str) -> str | None:
    for s in _stores():
        try:
            secret = s.get(provider)
        except Exception:
            continue
        if secret:
            return secret
    return None


def save(provider: str, secret: str) -> str:
    """Save to the OS secret store, falling back to the per-user file. Returns where it went."""
    if _NATIVE:
        try:
            _NATIVE.set(provider, secret)
            _FILE.delete(provider)  # don't leave an older plaintext copy behind
            return _NATIVE.name
        except Exception as e:
            print(f"  {_NATIVE.name} unavailable ({e}); using {_FILE.name}", file=sys.stderr)
    _FILE.set(provider, secret)
    return _FILE.name


def forget(provider: str) -> bool:
    removed = False
    for s in _stores():
        try:
            removed = s.delete(provider) or removed
        except Exception:
            pass
    return removed


# ---------------------------------------------------------------- login

def mask(secret: str) -> str:
    return f"{secret[:12]}...{secret[-4:]}" if len(secret) > 20 else "..."


def diagnose_key(provider: str, secret: str) -> str | None:
    """Explain an obviously wrong key from its shape. None if it looks like a plausible admin key."""
    p = PROVIDERS[provider]
    if "..." in secret or "\u2026" in secret:
        return ("This is the shortened key shown in the Console's key list (with \"...\" in the middle), not the key "
                "itself. The full key is only shown once, when it's created. Create a new admin key and copy it from "
                "that screen.")
    if secret != secret.strip() or any(c.isspace() for c in secret):
        return "The key contains spaces or line breaks. Copy it again so it's a single unbroken line."
    if not secret.isascii():
        return "The key contains hidden or non-standard characters (often from copying out of a document or chat). Copy it again from the provider's console."
    if provider == "anthropic":
        if secret.startswith("sk-ant-api"):
            return ("This key starts with sk-ant-api, so it's either a regular API key or a Claude Enterprise key. "
                    "If it came from claude.ai > Organization settings > API, paste it into the Claude Enterprise row "
                    "instead. This row is for Claude Console API usage and needs an Admin key (sk-ant-admin01-) "
                    "from platform.claude.com > Settings > Admin keys.")
        if secret.startswith(("sk-ant-oat", "sk-ant-ort")):
            return "This is a Claude login token, not an Admin key. Admin keys start with sk-ant-admin."
        if secret.startswith("sk-") and not secret.startswith("sk-ant-"):
            return "This looks like an OpenAI key. Paste it into the OpenAI row instead."
    if provider == "claude_enterprise":
        if secret.startswith("sk-ant-admin"):
            return ("This is a Claude Console Admin key. Paste it into the Anthropic API row. The Claude Enterprise row "
                    "needs a key from claude.ai > Organization settings > API (only the primary owner can create one) "
                    "with the read:analytics scope.")
        if secret.startswith(("sk-ant-oat", "sk-ant-ort")):
            return "This is a Claude login token, not an API key. Create one in claude.ai > Organization settings > API."
        if not secret.startswith("sk-ant-"):
            return "This doesn't look like an Anthropic key. Claude Enterprise keys start with sk-ant-api01-."
    if provider == "openai":
        if secret.startswith("sk-ant-"):
            return "This looks like an Anthropic key. Paste it into the Anthropic row instead."
        if secret.startswith(("sk-proj-", "sk-svcacct-", "sk-None-")) or (
                secret.startswith("sk-") and not secret.startswith("sk-admin-")):
            return ("This is a regular OpenAI API key. Usage reports need an Admin key, which starts with sk-admin-. "
                    "An organization owner can create one under Settings > Organization > Admin keys.")
    if secret.startswith(p["prefix"]) and len(secret) < 40:
        return "The key looks cut off. Copy the whole key; it's much longer than what was pasted."
    return None


def verify(provider: str, secret: str) -> None:
    """Raise ApiError if the provider rejects the key; returns quietly on success."""
    p = PROVIDERS[provider]
    params = p["verify_params"]() if "verify_params" in p else [("limit", "1")]
    http_get(p["verify"], p["headers"](secret), params)


def _prompt(provider: str) -> str:
    p = PROVIDERS[provider]
    print(f"\n{p['label']} admin API key required (starts with {p['prefix']}).", file=sys.stderr)
    print(f"  Create one at {p['where']}", file=sys.stderr)
    while True:
        secret = getpass.getpass(f"  {p['label']} admin key (input hidden, blank to skip): ").strip()
        if not secret:
            raise LookupError(f"{p['label']} skipped")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", secret):
            print("  That doesn't look like an API key (unexpected characters). Try again.", file=sys.stderr)
            continue
        hint = diagnose_key(provider, secret)
        if hint:
            print(f"  Warning: {hint}", file=sys.stderr)
        elif not secret.startswith(p["prefix"]):
            print(f"  Warning: {p['label']} admin keys normally start with {p['prefix']}; "
                  "a regular API key will be rejected.", file=sys.stderr)
        return secret


def _confirm(question: str) -> bool:
    ans = input(f"  {question} [Y/n] ").strip().lower()
    return ans in ("", "y", "yes")


def login(provider: str, interactive: bool, force_prompt: bool = False, remember: bool | None = None) -> dict[str, str]:
    """Return verified request headers for a provider, prompting/saving as needed."""
    p = PROVIDERS[provider]
    env_secret = os.environ.get(p["env"])
    if env_secret and not force_prompt:
        try:
            verify(provider, env_secret)
        except ApiError as e:
            raise SystemExit(f"{p['label']}: environment variable {p['env']} was rejected ({e.status}). Fix or unset it.") from None
        print(f"{p['label']}: authenticated from {p['env']} ({mask(env_secret)})", file=sys.stderr)
        return p["headers"](env_secret)

    saved = None if force_prompt else load_saved(provider)
    if saved:
        try:
            verify(provider, saved)
            print(f"{p['label']}: authenticated with saved key ({mask(saved)})", file=sys.stderr)
            return p["headers"](saved)
        except ApiError as e:
            if e.status not in (401, 403):
                raise
            print(f"{p['label']}: saved key was rejected ({e.status}); it may have been revoked.", file=sys.stderr)

    if not interactive:
        raise SystemExit(f"{p['label']}: no valid credential. Run `python3 fetch_usage.py login {provider}` "
                         f"in a terminal or set the {p['env']} environment variable.")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        secret = _prompt(provider)
        try:
            verify(provider, secret)
        except ApiError as e:
            hint = "not an admin key, or revoked" if e.status in (401, 403) else str(e)
            print(f"  Login failed ({e.status}: {hint}). Attempt {attempt}/{MAX_ATTEMPTS}.", file=sys.stderr)
            continue
        print(f"  {p['label']}: logged in ({mask(secret)})", file=sys.stderr)
        if remember if remember is not None else _confirm("Save this key for next time?"):
            where = save(provider, secret)
            print(f"  Saved to {where}", file=sys.stderr)
        return p["headers"](secret)
    raise LookupError(f"{p['label']}: too many failed attempts")


def status(provider: str) -> str:
    p = PROVIDERS[provider]
    for source, secret in ((p["env"], os.environ.get(p["env"])), ("saved", load_saved(provider))):
        if not secret:
            continue
        try:
            verify(provider, secret)
            return f"{p['label']:<10} OK       {source} ({mask(secret)})"
        except ApiError as e:
            return f"{p['label']:<10} REJECTED {source} ({mask(secret)}): HTTP {e.status}"
        except urllib.error.URLError as e:
            return f"{p['label']:<10} UNKNOWN  {source} ({mask(secret)}): {e.reason}"
    return f"{p['label']:<10} not logged in"
