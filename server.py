import base64
import hashlib
import json
import hmac
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI


MANIFEST_JSON = """{
"name":"AI Chat",
"short_name":"AI Chat",
"id":"/",
"start_url":"/",
"scope":"/",
"display":"standalone",
"background_color":"#0b1120",
"theme_color":"#111827",
"icons":[{"src":"/icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any"}]
}"""

SERVICE_WORKER_JS = """const CACHE="chat-app-v9";
const ASSETS=["/","/style.css","/app.js","/manifest.webmanifest","/icon.svg"];
self.addEventListener("install",(event)=>{
event.waitUntil(caches.open(CACHE).then((cache)=>cache.addAll(ASSETS)));
self.skipWaiting();
});
self.addEventListener("activate",(event)=>{
event.waitUntil(caches.keys().then((keys)=>Promise.all(keys.filter((key)=>key!==CACHE).map((key)=>caches.delete(key)))));
self.clients.claim();
});
self.addEventListener("fetch",(event)=>{
const request=event.request;
if(request.method!=="GET"||new URL(request.url).pathname.startsWith("/api/"))return;
event.respondWith(fetch(request).catch(()=>caches.match(request)));
});"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#111827"/><path d="M136 168h240a40 40 0 0 1 40 40v96a40 40 0 0 1-40 40H236l-72 56v-56h-28a40 40 0 0 1-40-40v-96a40 40 0 0 1 40-40z" fill="#22d3ee"/></svg>"""

STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

RELAY_DEBUG_IDENTITY_FIELDS = {
    "id",
    "model",
    "provider",
    "metadata",
    "object",
    "created",
    "system_fingerprint",
    "service_tier",
}
RELAY_DEBUG_SECRET_KEYS = {
    "authorization",
    "proxy-authorization",
    "api-key",
    "api_key",
    "x-api-key",
    "cookie",
    "set-cookie",
    "access_token",
    "refresh_token",
    "token",
    "password",
    "credential",
    "secret",
}


LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60.0
RELAY_URL_MAX_LENGTH = 512
REQUEST_TARGET_MAX_LENGTH = 253
MAX_REQUEST_BODY_BYTES = 1024 * 1024
SESSION_COOKIE_NAME = "chat_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
FILE_PRIVATE_MODE = 0o600
SECRET_SERVICE_NAME = "chat-app-api-key"
SECRET_HELPER_TIMEOUT_SECONDS = 5.0
SECRET_FILE_BACKEND = "file"
SECRET_HELPER_BACKENDS = {"macos-keychain", "libsecret"}
UNIQUE_LOCAL_IPV6_PREFIX_BYTES = {0xFC, 0xFD}
BLOCKED_RELAY_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
    "metadata.arm.cloud",
}
RATE_LIMIT_WINDOWS = {
    "chat": (30, 60.0),
    "config_write": (20, 60.0),
}
RATE_LIMIT_TRACKED_KEYS = 512


def normalize_models(models):
    normalized = []
    seen = set()
    for model in models or []:
        if not isinstance(model, str):
            continue
        model = model.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        normalized.append(model)
    return normalized


def normalize_api_key(api_key):
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("API Key 不能为空")
    api_key = api_key.strip()
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("API Key 只能包含英文字符和数字，请只粘贴中转站提供的 Key，不要包含中文标签或全角符号") from error
    if any(ord(character) < 33 or ord(character) > 126 for character in api_key):
        raise ValueError("API Key 包含空格或不可见字符，请重新复制中转站提供的 Key")
    return api_key


def is_loopback_host(host):
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def ip_literal(host):
    try:
        address = ip_address(host.strip("[]"))
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def is_blocked_relay_host(host):
    address = ip_literal(host)
    if address is None or address.is_loopback:
        return False
    if address.version == 6 and address.packed[0] in UNIQUE_LOCAL_IPV6_PREFIX_BYTES:
        return True
    return (
        address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_relay_base_url(base_url):
    if not isinstance(base_url, str):
        raise ValueError("API URL 必须以 http:// 或 https:// 开头")
    candidate = base_url.strip()
    if not candidate:
        raise ValueError("API URL 不能为空")
    if len(candidate) > RELAY_URL_MAX_LENGTH:
        raise ValueError("API URL 过长")
    if re.search(r"[\s\x00-\x1f\x7f]", candidate):
        raise ValueError("API URL 不能包含空格或不可见字符")
    if not candidate.startswith(("http://", "https://")):
        raise ValueError("API URL 必须以 http:// 或 https:// 开头")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as error:
        raise ValueError("API URL 格式错误") from error
    if parts.scheme not in {"http", "https"}:
        raise ValueError("API URL 必须以 http:// 或 https:// 开头")
    if parts.username is not None or parts.password is not None:
        raise ValueError("API URL 不能包含用户名或密码")
    if not parts.hostname:
        raise ValueError("API URL 缺少有效的主机名")
    if is_blocked_relay_host(parts.hostname):
        raise ValueError("API URL 不能指向链路本地、组播或保留地址")
    if relay_host_is_blocked_by_address(parts.hostname):
        raise ValueError("API URL 的域名解析到了不允许的地址")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("API URL 端口无效")
    return candidate


def allowed_extra_hosts():
    return {
        item.strip().rstrip(".").lower()
        for item in os.environ.get("ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }


def is_trusted_host_header(host_header):
    if not host_header or len(host_header) > REQUEST_TARGET_MAX_LENGTH:
        return False
    try:
        hostname = urlsplit("//" + host_header.strip()).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    if ip_literal(hostname) is not None:
        return True
    return hostname in allowed_extra_hosts()


def env_flag(name, default=True):
    value = os.environ.get(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def write_private_text(path, text):
    """Write a file atomically with owner-only permissions (POSIX)."""
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        if os.name == "nt":
            with temp_path.open("w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PRIVATE_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        restrict_file_permissions(path)
    finally:
        temp_path.unlink(missing_ok=True)


def restrict_file_permissions(path):
    """Tighten a sensitive file to owner read/write. Windows uses inherited ACLs."""
    if os.name == "nt":
        return False
    try:
        os.chmod(path, FILE_PRIVATE_MODE)
    except OSError:
        return False
    return True


def secret_backend_name():
    configured = os.environ.get("SECRET_BACKEND", "").strip().lower()
    if configured in {"file", "windows-dpapi", "macos-keychain", "libsecret"}:
        return configured
    if os.name == "nt":
        return "windows-dpapi"
    if sys.platform == "darwin":
        return "macos-keychain" if shutil.which("security") else SECRET_FILE_BACKEND
    return "libsecret" if shutil.which("secret-tool") else SECRET_FILE_BACKEND


def secret_reference(path):
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return SECRET_SERVICE_NAME, f"{SECRET_SERVICE_NAME}:{digest}"


def secret_store_arguments(backend, service, account):
    if backend == "macos-keychain":
        return ["security", "add-generic-password", "-a", account, "-s", service, "-U", "-w"]
    return [
        "secret-tool",
        "store",
        "--label",
        f"Chat App API Key ({account})",
        "service",
        service,
        "account",
        account,
    ]


def secret_delete_arguments(backend, service, account):
    if backend == "macos-keychain":
        return ["security", "delete-generic-password", "-a", account, "-s", service]
    return ["secret-tool", "clear", "service", service, "account", account]


def secret_lookup_arguments(backend, service, account):
    if backend == "macos-keychain":
        return ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    return ["secret-tool", "lookup", "service", service, "account", account]


def run_secret_command(arguments, secret=None):
    """Run an OS credential helper. The secret travels on stdin, never in argv."""
    payload = secret.encode("utf-8") if isinstance(secret, str) else secret
    try:
        completed = subprocess.run(
            list(arguments),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=SECRET_HELPER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, b""
    return completed.returncode, completed.stdout or b""


def resolve_host_addresses(host):
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    return {info[4][0] for info in infos}


def relay_host_is_blocked_by_address(host, resolver=None):
    if host.lower() in BLOCKED_RELAY_HOSTNAMES:
        return True
    if not env_flag("RELAY_CHECK_RESOLVED_ADDRESS", True):
        return False
    if ip_literal(host) is not None:
        return False
    addresses = (resolver or resolve_host_addresses)(host)
    if not addresses:
        return False
    return any(is_blocked_relay_host(address) for address in addresses)


class RateLimiter:
    """Fixed-window counter per action and client key."""

    def __init__(self, windows):
        self.windows = dict(windows)
        self.events = {}
        self.lock = threading.Lock()

    def check(self, action, key, now=None):
        limit, window = self.windows.get(action, (0, 0.0))
        if limit <= 0:
            return True, 0.0
        now = time.time() if now is None else now
        identity = f"{action}:{key}"
        with self.lock:
            stamps = [stamp for stamp in self.events.get(identity, ()) if now - stamp < window]
            if len(stamps) >= limit:
                self.events[identity] = stamps
                return False, max(0.0, window - (now - stamps[0]))
            stamps.append(now)
            self.events[identity] = stamps
            self._prune(now)
        return True, 0.0

    def _prune(self, now):
        if len(self.events) <= RATE_LIMIT_TRACKED_KEYS:
            return
        for identity in list(self.events):
            action = identity.split(":", 1)[0]
            window = self.windows.get(action, (0, 0.0))[1]
            stamps = self.events[identity]
            if not stamps or now - stamps[-1] >= window:
                self.events.pop(identity, None)


def relay_debug_enabled():
    return os.environ.get("RELAY_DEBUG_RESPONSE", "").strip().lower() in {"1", "true", "yes", "on"}


def redact_relay_debug(value, api_key=None):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_") in RELAY_DEBUG_SECRET_KEYS
            else redact_relay_debug(item, api_key)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_relay_debug(item, api_key) for item in value]
    if isinstance(value, str):
        redacted = value.replace(api_key, "[REDACTED]") if api_key else value
        return redact_sensitive_text(redacted)
    return value


SENSITIVE_KEY_PATTERN = (
    r"(?i)([\x27\x22]?(?:authorization|proxy-authorization|api[_-]?key|access[_-]?key"
    r"|access_token|refresh_token|session_token|secret|token|password|passwd|cookie"
    r"|set-cookie|chat_session)[\x27\x22]?\s*[:=]\s*)"
)
SENSITIVE_QUOTED_PATTERN = re.compile(
    SENSITIVE_KEY_PATTERN + r"([\x27\x22])(?:[^\x27\x22]|\\[\x27\x22])*?\2"
)
SENSITIVE_BARE_PATTERN = re.compile(SENSITIVE_KEY_PATTERN + r"[^\x27\x22\s,;}]+")

LOCAL_PATH_PATTERNS = {}


def local_path_markers():
    markers = set()
    for value in (os.path.expanduser("~"), os.getcwd(), str(Path(__file__).parent)):
        cleaned = (value or "").rstrip("\\ /")
        if len(cleaned) >= 3:
            markers.add(cleaned)
    return sorted(markers, key=len, reverse=True)


def local_path_pattern(prefix):
    cached = LOCAL_PATH_PATTERNS.get(prefix)
    if cached is not None:
        return cached
    pieces = []
    for chunk in re.split(r"([\\/])", prefix.rstrip("\\ /")):
        if chunk in ("\\", "/"):
            pieces.append(r"[\\/]")
        elif chunk:
            pieces.append(re.escape(chunk))
    pieces.append(r"(?:[\\/][^\x27\x22\s,;:)}]*)?")
    compiled = re.compile("".join(pieces), re.IGNORECASE if os.name == "nt" else 0)
    LOCAL_PATH_PATTERNS[prefix] = compiled
    return compiled


def redact_local_paths(text):
    lowered = text.lower()
    for prefix in local_path_markers():
        if prefix.lower() in lowered:
            text = local_path_pattern(prefix).sub("[PATH]", text)
            lowered = text.lower()
    return text


def redact_sensitive_text(text):
    redacted = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    redacted = re.sub(r"(?i)\bsk-[A-Za-z0-9._-]{8,}", "[REDACTED]", redacted)
    redacted = SENSITIVE_QUOTED_PATTERN.sub(
        lambda match: match.group(1) + match.group(2) + "[REDACTED]" + match.group(2),
        redacted,
    )
    redacted = SENSITIVE_BARE_PATTERN.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
    return redact_local_paths(redacted)


def describe_error(error, api_key=None, extra_secrets=()):
    message = redact_local_paths(str(error).strip() or type(error).__name__)
    for secret in extra_secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return redact_relay_debug(message, api_key)


def dpapi_transformation(function, data):
    """Call a Win32 protect/unprotect entry point and return its raw output."""
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DataBlob()
    if not function(
        ctypes.byref(input_blob), None, None, None, None, 0x1, ctypes.byref(output_blob)
    ):
        return None
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.data)


def dpapi_protect(data):
    """Encrypt bytes for the current Windows user, or return None."""
    if os.name != "nt":
        return None
    import ctypes

    return dpapi_transformation(ctypes.windll.crypt32.CryptProtectData, data)


def dpapi_unprotect(data):
    """Decrypt a DPAPI blob for the current Windows user, or return None."""
    if os.name != "nt":
        return None
    import ctypes

    return dpapi_transformation(ctypes.windll.crypt32.CryptUnprotectData, data)


class AppConfigStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).with_name("app-config.json")
        self.lock = threading.Lock()

    def load(self):
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def save(self, data):
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with self.lock:
            write_private_text(self.path, payload)


class SecretStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).with_name("app-secret.json")
        self.lock = threading.Lock()

    def _protect(self, value):
        data = value.encode("utf-8")
        backend = secret_backend_name()
        if backend in SECRET_HELPER_BACKENDS:
            service, account = secret_reference(self.path)
            run_secret_command(secret_delete_arguments(backend, service, account))
            returncode, _output = run_secret_command(
                secret_store_arguments(backend, service, account), secret=value
            )
            if returncode == 0:
                return backend, account
            backend = SECRET_FILE_BACKEND
        if backend == "windows-dpapi" and os.name == "nt":
            protected = dpapi_protect(data)
            if protected is None:
                raise OSError("Unable to protect API Key")
            return "windows-dpapi", base64.b64encode(protected).decode("ascii")
        return SECRET_FILE_BACKEND, base64.b64encode(data).decode("ascii")

    def _unprotect(self, protection, value):
        if not isinstance(protection, str) or not isinstance(value, str) or not value:
            return None
        if protection in SECRET_HELPER_BACKENDS:
            service, account = secret_reference(self.path)
            if value != account:
                return None
            returncode, output = run_secret_command(
                secret_lookup_arguments(protection, service, account)
            )
            if returncode != 0:
                return None
            return output.decode("utf-8", "ignore").strip() or None
        protected = base64.b64decode(value.encode("ascii"), validate=True)
        if protection == SECRET_FILE_BACKEND:
            return protected.decode("utf-8", "ignore") or None
        if protection != "windows-dpapi":
            return None
        recovered = dpapi_unprotect(protected)
        return recovered.decode("utf-8", "ignore") if recovered else None

    def load(self):
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                return None
            value = self._unprotect(data.get("protection"), data.get("value"))
            return normalize_api_key(value) if value else None
        except (OSError, ValueError, TypeError, AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def save(self, api_key):
        protection, value = self._protect(normalize_api_key(api_key))
        payload = json.dumps({"version": 1, "protection": protection, "value": value}, indent=2)
        with self.lock:
            write_private_text(self.path, payload)


class ChatServer:
    def __init__(self, app_password=None, config_path=None):
        self.client = None
        self.conversations = ConversationStore()
        self.config_store = AppConfigStore(config_path)
        secret_path = self.config_store.path.with_name(
            "app-secret.json" if config_path is None else f"{self.config_store.path.stem}.secret.json"
        )
        self.secret_store = SecretStore(secret_path)
        self.config_lock = threading.Lock()
        saved_config = self.config_store.load()
        stored_api_key = self.secret_store.load()
        legacy_api_key = saved_config.get("api_key")
        if not stored_api_key and legacy_api_key:
            try:
                stored_api_key = normalize_api_key(legacy_api_key)
                self.secret_store.save(stored_api_key)
            except (OSError, ValueError):
                stored_api_key = None
        if stored_api_key and legacy_api_key is not None:
            saved_config.pop("api_key", None)
            self.config_store.save(saved_config)
        self.api_key = os.environ.get("OPENAI_API_KEY") or stored_api_key
        self.base_url = os.environ.get("OPENAI_BASE_URL") or saved_config.get("base_url")
        configured_models = os.environ.get("OPENAI_MODELS")
        self.configured_models = normalize_models(
            configured_models.split(",") if configured_models else saved_config.get("models", [])
        )
        self.discovered_models = []
        self.default_model = (
            os.environ.get("OPENAI_MODEL")
            or saved_config.get("default_model")
            or (self.configured_models[0] if self.configured_models else None)
        )
        self.app_password = app_password if app_password is not None else os.environ.get("APP_PASSWORD")
        self.session_lock = threading.Lock()
        self.sessions = {}
        self.login_lock = threading.Lock()
        self.login_failures = {}
        self.rate_limiter = RateLimiter(RATE_LIMIT_WINDOWS)

    def auth_required(self):
        return bool(self.app_password)

    def authenticate(self, password):
        return bool(
            self.app_password
            and isinstance(password, str)
            and hmac.compare_digest(password, self.app_password)
        )

    def create_session(self):
        token = secrets.token_urlsafe(32)
        with self.session_lock:
            self.sessions[token] = time.time() + SESSION_MAX_AGE_SECONDS
        return token

    def is_session_valid(self, token):
        if not self.auth_required():
            return True
        if not token:
            return False
        now = time.time()
        with self.session_lock:
            expires_at = self.sessions.get(token)
            if expires_at is None or expires_at <= now:
                self.sessions.pop(token, None)
                return False
            self.sessions[token] = now + SESSION_MAX_AGE_SECONDS
        return True

    def revoke_session(self, token):
        if token:
            with self.session_lock:
                self.sessions.pop(token, None)

    def login_lockout_remaining(self, source_ip):
        if not source_ip:
            return 0.0
        with self.login_lock:
            record = self.login_failures.get(source_ip)
            if not record:
                return 0.0
            remaining = record["locked_until"] - time.time()
            if remaining <= 0:
                if not record["failures"]:
                    self.login_failures.pop(source_ip, None)
                return 0.0
            return remaining

    def register_login_failure(self, source_ip):
        if not source_ip:
            return
        with self.login_lock:
            record = self.login_failures.get(source_ip) or {"failures": 0, "locked_until": 0.0}
            if record["locked_until"] > time.time():
                return
            record["failures"] += 1
            if record["failures"] >= LOGIN_MAX_FAILED_ATTEMPTS:
                record["failures"] = 0
                record["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
            self.login_failures[source_ip] = record

    def clear_login_failures(self, source_ip):
        with self.login_lock:
            self.login_failures.pop(source_ip, None)

    def configure(
        self,
        api_key,
        base_url,
        models=None,
        default_model=None,
        persist=True,
        persist_api_key=True,
    ):
        api_key = normalize_api_key(api_key)
        base_url = validate_relay_base_url(base_url)
        with self.config_lock:
            self.api_key = api_key
            self.base_url = base_url
            self.client = None
            self.discovered_models = []
            if models is not None:
                self.configured_models = normalize_models(models)
            if default_model is not None:
                self.default_model = default_model.strip() or None
            if persist:
                if persist_api_key:
                    self.secret_store.save(self.api_key)
                self.config_store.save({
                    "base_url": self.base_url,
                    "models": self.configured_models,
                    "default_model": self.default_model,
                })

    def select_default_model(self, model, models=None):
        if not isinstance(model, str) or not model.strip():
            raise ValueError("No model selected")
        model = model.strip()
        with self.config_lock:
            self.default_model = model
            configured_models = self.configured_models if models is None else normalize_models(models)
            self.configured_models = normalize_models([*configured_models, model])
            self.config_store.save({
                "base_url": self.base_url,
                "models": self.configured_models,
                "default_model": self.default_model,
            })

    def rate_limit_key(self, token, source_ip):
        if token:
            return "session:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return "ip:" + (source_ip or "unknown")

    def key_reveal_allowed(self):
        return env_flag("ALLOW_KEY_REVEAL", True)

    def reveal_api_key(self):
        with self.config_lock:
            return self.api_key or ""

    def safe_error_message(self, error):
        with self.config_lock:
            api_key = self.api_key or ""
        return describe_error(error, api_key, extra_secrets=(self.app_password or "",))

    def config_summary(self):
        with self.config_lock:
            api_key = self.api_key or ""
            return {
                "configured": bool(api_key and self.base_url),
                "api_key_saved": bool(api_key),
                "api_key_hint": f"{api_key[:4]}…{api_key[-4:]}" if len(api_key) >= 8 else ("已保存" if api_key else ""),
                "base_url": self.base_url or "",
                "models": list(self.configured_models),
                "default_model": self.default_model,
                "key_reveal_enabled": self.key_reveal_allowed(),
            }

    def ensure_client(self):
        with self.config_lock:
            if self.api_key and self.base_url and self.client is None:
                self.client = OpenAI(api_key=normalize_api_key(self.api_key), base_url=self.base_url)
            client = self.client
        if client is None:
            raise RuntimeError("请先在左侧栏“连接设置”里填写 API URL 和 Key")
        return client

    def list_models(self):
        client = self.ensure_client()
        try:
            discovered = normalize_models(model.id for model in client.models.list())
            with self.config_lock:
                self.discovered_models = discovered
        except Exception:
            with self.config_lock:
                discovered = list(self.discovered_models)
        models = normalize_models([*discovered, *self.configured_models])
        source = "relay" if discovered else "manual"
        return {"models": models, "source": source, "default_model": self.default_model}

    def is_configured(self):
        return bool(self.api_key and self.base_url)

    def stream_chat(self, messages, model):
        client = self.ensure_client()
        if relay_debug_enabled():
            with client.chat.completions.with_streaming_response.create(
                model=model,
                messages=messages,
                stream=True,
            ) as raw_response:
                headers = redact_relay_debug(dict(raw_response.http_response.headers), self.api_key)
                self.log_relay_debug("response", {
                    "requested_model": model,
                    "status": raw_response.http_response.status_code,
                    "headers": headers,
                })
                response = raw_response.parse()
                yield from self.stream_relay_chunks(response)
            return

        response = client.chat.completions.create(model=model, messages=messages, stream=True)
        yield from self.stream_relay_chunks(response, debug=False)

    def stream_relay_chunks(self, response, debug=True):
        identity = {}
        for index, chunk in enumerate(response):
            if debug:
                payload = redact_relay_debug(chunk.model_dump(mode="json", exclude_unset=True), self.api_key)
                self.log_relay_debug("chunk", {"index": index, "data": payload})
                for key in RELAY_DEBUG_IDENTITY_FIELDS:
                    if key in payload and payload[key] is not None:
                        identity[key] = payload[key]
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield {"content": content}
        if debug:
            self.log_relay_debug("identity_summary", identity)

    def log_relay_debug(self, event, data):
        print("[relay-response-debug] " + json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)

    def chat_with_memory(self, conversation_id, messages, model):
        if not conversation_id:
            conversation_id = uuid.uuid4().hex
        self.conversations.save_messages(conversation_id, messages, model)
        yield {"conversation_id": conversation_id}
        reply_parts = []
        try:
            for part in self.stream_chat(messages, model):
                content = part.get("content", "")
                if content:
                    reply_parts.append(content)
                yield part
        finally:
            reply = "".join(reply_parts)
            if reply:
                saved_messages = messages + [{"role": "assistant", "content": reply}]
                self.conversations.save_messages(conversation_id, saved_messages, model)


class ConversationStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).with_name('chat-history.json')
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        try:
            with self.path.open('r', encoding='utf-8') as file:
                data = json.load(file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {'version': 1, 'conversations': {}}
        if not isinstance(data, dict) or not isinstance(data.get('conversations'), dict):
            return {'version': 1, 'conversations': {}}
        conversations = {}
        for conversation_id, item in data['conversations'].items():
            if not isinstance(conversation_id, str) or not isinstance(item, dict):
                continue
            messages = item.get('messages')
            if not isinstance(messages, list):
                continue
            valid_messages = [
                {'role': message['role'], 'content': message['content']}
                for message in messages
                if isinstance(message, dict)
                and message.get('role') in {'user', 'assistant'}
                and isinstance(message.get('content'), str)
            ]
            created_at = item.get('created_at')
            updated_at = item.get('updated_at')
            if not isinstance(created_at, (int, float)) or not isinstance(updated_at, (int, float)):
                continue
            title = item.get('title')
            model = item.get('model')
            conversations[conversation_id] = {
                'id': conversation_id,
                'title': title if isinstance(title, str) and title else '新对话',
                'created_at': created_at,
                'updated_at': updated_at,
                'messages': valid_messages,
                'model': model if isinstance(model, str) and model else None,
            }
        return {'version': 1, 'conversations': conversations}

    def _save(self):
        write_private_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))

    def list_conversations(self):
        with self.lock:
            conversations = [
                {
                    'id': item['id'],
                    'title': item['title'],
                    'created_at': item['created_at'],
                    'updated_at': item['updated_at'],
                    'message_count': len(item['messages']),
                    'model': item.get('model'),
                }
                for item in self.data['conversations'].values()
            ]
        return sorted(conversations, key=lambda item: item['updated_at'], reverse=True)

    def get_conversation(self, conversation_id):
        with self.lock:
            return self.data['conversations'].get(conversation_id)

    def append_message(self, conversation_id, role, content):
        with self.lock:
            conversation = self.data['conversations'].get(conversation_id)
            if conversation is None:
                if not conversation_id:
                    conversation_id = uuid.uuid4().hex
                timestamp = time.time()
                title = content.strip().splitlines()[0][:40] if content.strip() else '新对话'
                conversation = {
                    'id': conversation_id,
                    'title': title,
                    'created_at': timestamp,
                    'updated_at': timestamp,
                    'messages': [],
                }
                self.data['conversations'][conversation_id] = conversation
            conversation['messages'].append({'role': role, 'content': content})
            conversation['updated_at'] = time.time()
            if role == 'user' and conversation['title'] == '新对话' and content.strip():
                conversation['title'] = content.strip().splitlines()[0][:40]
            self._save()
            return conversation_id

    def save_messages(self, conversation_id, messages, model=None):
        content = ''
        for item in messages:
            if item.get('role') == 'user':
                content = item.get('content', '')
                break
        title = content.strip().splitlines()[0][:40] if content.strip() else '新对话'
        timestamp = time.time()
        conversation = {
            'id': conversation_id,
            'title': title,
            'created_at': timestamp,
            'updated_at': timestamp,
            'messages': messages,
            'model': model,
        }
        with self.lock:
            existing = self.data['conversations'].get(conversation_id)
            if existing:
                conversation['created_at'] = existing.get('created_at', timestamp)
                existing['title'] = title
                existing['messages'] = messages
                existing['updated_at'] = timestamp
                existing['model'] = model or existing.get('model')
            else:
                self.data['conversations'][conversation_id] = conversation
            self._save()

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ChatApp/1.0"
    chat_server = None

    def send_bytes(self, status, content_type, body, cache_control="no-cache", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_bytes(status, "application/json; charset=utf-8", body, "no-store", extra_headers)

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def secure_cookies_requested(self):
        """Secure cookies are opt-in per environment, or automatic behind HTTPS."""
        configured = os.environ.get("COOKIE_SECURE", "").strip().lower()
        if configured in {"1", "true", "yes", "on"}:
            return True
        if configured in {"0", "false", "no", "off"}:
            return False
        forwarded = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return forwarded == "https"

    def session_cookie(self, token, max_age=SESSION_MAX_AGE_SECONDS):
        attributes = [
            f"{SESSION_COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max_age}",
        ]
        if self.secure_cookies_requested():
            attributes.append("Secure")
        return "; ".join(attributes)

    def require_rate_limit(self, action):
        token = self.session_token() or ""
        source_ip = self.client_address[0] if self.client_address else ""
        allowed, retry_after = self.chat_server.rate_limiter.check(
            action, self.chat_server.rate_limit_key(token, source_ip)
        )
        if allowed:
            return True
        return self.reject_request(
            429,
            "请求过于频繁，请稍后再试",
            {"Retry-After": str(int(retry_after) + 1)},
        )

    def is_authenticated(self):
        return self.chat_server.is_session_valid(self.session_token())

    def require_authentication(self):
        if self.is_authenticated():
            return True
        self.send_json(401, {"error": "Authentication required"})
        return False

    def drain_request_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = -1
        if not 0 <= length <= MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            return
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                self.close_connection = True
                return
            length -= len(chunk)

    def reject_request(self, status, message, extra_headers=None):
        self.drain_request_body()
        self.send_json(status, {"error": message}, extra_headers)
        return False

    def require_trusted_host(self):
        if is_trusted_host_header(self.headers.get("Host", "")):
            return True
        return self.reject_request(
            400,
            "Host 不被信任，请通过本机 IP 或 ALLOWED_HOSTS 中列出的域名访问",
        )

    def is_same_origin_write(self):
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return (self.headers.get("Sec-Fetch-Site") or "").strip().lower() != "cross-site"
        if origin == "null":
            return False
        try:
            authority = urlsplit(origin).netloc
        except ValueError:
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(authority) and authority.lower() == host

    def require_same_origin_write(self):
        if self.is_same_origin_write():
            return True
        return self.reject_request(403, "跨站请求已被拒绝，请从本应用页面操作")

    def require_json_body_type(self):
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type in {"", "application/json"}:
            return True
        return self.reject_request(415, "请求体格式必须是 application/json")

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BODY_BYTES:
                return None
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    def do_GET(self):
        if not self.require_trusted_host():
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True, "auth_required": self.chat_server.auth_required()})
        elif path == "/api/session":
            self.send_json(200, {"authenticated": self.is_authenticated(), "auth_required": self.chat_server.auth_required()})
        elif path.startswith("/api/") and not self.require_authentication():
            return
        elif path == "/api/config":
            self.send_json(200, self.chat_server.config_summary())
        elif path == "/api/config/key":
            if not self.chat_server.key_reveal_allowed():
                self.send_json(404, {"error": "Not found"})
            elif (self.headers.get("X-Reveal-Api-Key") or "").strip() != "1":
                self.send_json(400, {"error": "需要显式确认后才能显示 API Key"})
            else:
                api_key = self.chat_server.reveal_api_key()
                if not api_key:
                    self.send_json(404, {"error": "No saved API Key"})
                else:
                    self.send_json(
                        200,
                        {"api_key": api_key},
                        {"Cross-Origin-Resource-Policy": "same-origin"},
                    )
        elif path == "/api/conversations":
            self.send_json(200, {"conversations": self.chat_server.conversations.list_conversations()})
        elif path == "/api/models":
            if not self.chat_server.is_configured():
                self.send_json(409, {"error": "请先在左侧栏“连接设置”里配置 API URL 和 Key"})
            else:
                try:
                    self.send_json(200, self.chat_server.list_models())
                except Exception as error:
                    self.send_json(502, {"error": self.chat_server.safe_error_message(error)})
        elif path.startswith("/api/conversations/"):
            conversation_id = path.rsplit("/", 1)[1]
            conversation = self.chat_server.conversations.get_conversation(conversation_id)
            if conversation is None:
                self.send_json(404, {"error": "Conversation not found"})
            else:
                self.send_json(200, conversation)
        elif path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            try:
                body = (STATIC_DIR / filename).read_bytes()
            except FileNotFoundError:
                self.send_json(500, {"error": "Static asset not found"})
            else:
                self.send_bytes(200, content_type, body)
        elif path == "/manifest.webmanifest":
            self.send_bytes(200, "application/manifest+json; charset=utf-8", MANIFEST_JSON.encode("utf-8"))
        elif path == "/sw.js":
            self.send_bytes(200, "text/javascript; charset=utf-8", SERVICE_WORKER_JS.encode("utf-8"))
        elif path == "/icon.svg":
            self.send_bytes(200, "image/svg+xml", ICON_SVG.encode("utf-8"))
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.require_trusted_host():
            return
        if not self.require_same_origin_write():
            return
        if not self.require_json_body_type():
            return
        path = urlsplit(self.path).path
        if path == "/api/login":
            source_ip = self.client_address[0] if self.client_address else ""
            lockout_remaining = self.chat_server.login_lockout_remaining(source_ip)
            if lockout_remaining > 0:
                self.reject_request(
                    429,
                    "登录失败次数过多，请稍后再试",
                    {"Retry-After": str(int(lockout_remaining) + 1)},
                )
                return
            body = self.read_json_body()
            if body is None or not self.chat_server.authenticate(body.get("password")):
                self.chat_server.register_login_failure(source_ip)
                self.send_json(401, {"error": "密码不正确"})
                return
            self.chat_server.clear_login_failures(source_ip)
            token = self.chat_server.create_session()
            self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie(token)})
            return
        if path == "/api/logout":
            self.chat_server.revoke_session(self.session_token())
            self.send_json(
                200,
                {"ok": True},
                {"Set-Cookie": self.session_cookie("", max_age=0)},
            )
            return
        if path.startswith("/api/") and not self.require_authentication():
            return
        if path in {"/api/config", "/api/config/model"} and not self.require_rate_limit("config_write"):
            return
        if path == "/api/chat" and not self.require_rate_limit("chat"):
            return
            return
        if path == "/api/config":
            body = self.read_json_body()
            if body is None:
                self.send_json(400, {"error": "Invalid JSON body"})
                return
            api_key = body.get("api_key")
            base_url = body.get("base_url")
            models = body.get("models")
            default_model = body.get("default_model")
            key_provided = isinstance(api_key, str) and bool(api_key.strip())
            if not key_provided:
                api_key = self.chat_server.api_key
            try:
                api_key = normalize_api_key(api_key)
                base_url = validate_relay_base_url(base_url)
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            if models is not None and (not isinstance(models, list) or not all(isinstance(item, str) and item.strip() for item in models)):
                self.send_json(400, {"error": "模型格式错误"})
                return
            if default_model is not None and not isinstance(default_model, str):
                self.send_json(400, {"error": "模型格式错误"})
                return
            models = normalize_models(models)
            default_model = (default_model or "").strip() or (models[0] if models else None)
            models = normalize_models([*models, default_model] if default_model else models)
            try:
                self.chat_server.configure(
                    api_key,
                    base_url,
                    models,
                    default_model,
                    persist_api_key=key_provided,
                )
            except OSError as error:
                self.send_json(500, {"error": self.chat_server.safe_error_message(error)})
                return
            self.send_json(200, {"ok": True, "configured": True})
            return
        if path == "/api/config/model":
            body = self.read_json_body()
            if body is None:
                self.send_json(400, {"error": "Invalid JSON body"})
                return
            models = body.get("models")
            if models is not None and (not isinstance(models, list) or not all(isinstance(item, str) and item.strip() for item in models)):
                self.send_json(400, {"error": "Invalid model list"})
                return
            try:
                self.chat_server.select_default_model(body.get("model"), models)
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            except OSError as error:
                self.send_json(500, {"error": self.chat_server.safe_error_message(error)})
                return
            self.send_json(200, {"ok": True, "default_model": self.chat_server.default_model})
            return
        if path != "/api/chat":
            self.send_json(404, {"error": "Not found"})
            return

        body = self.read_json_body()
        if body is None:
            self.send_json(400, {"error": "Invalid JSON body"})
            return

        conversation_id = body.get("conversation_id")
        messages = body.get("messages")
        model = body.get("model") or self.chat_server.default_model
        if not self.chat_server.is_configured():
            self.send_json(409, {"error": "请先在左侧栏“连接设置”里配置 API URL 和 Key"})
            return
        if conversation_id and not isinstance(conversation_id, str):
            self.send_json(400, {"error": "conversation_id must be a string"})
            return
        if not isinstance(messages, list) or not messages:
            self.send_json(400, {"error": "messages must be a non-empty list"})
            return
        if not all(isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str) for item in messages):
            self.send_json(400, {"error": "Invalid messages"})
            return
        if not isinstance(model, str) or not model.strip():
            self.send_json(400, {"error": "No model selected"})
            return
        model = model.strip()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            for part in self.chat_server.chat_with_memory(conversation_id, messages, model):
                data = "data: " + json.dumps(part, ensure_ascii=False) + "\n\n"
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()
        except Exception as error:
            if isinstance(error, (BrokenPipeError, ConnectionResetError)):
                return
            try:
                payload = {"error": self.chat_server.safe_error_message(error)}
                data = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                self.wfile.write(data.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format, *args):
        pass


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    app_password = os.environ.get("APP_PASSWORD")
    is_loopback = is_loopback_host(host)
    if not is_loopback and not app_password:
        print("Refusing to expose the app on the network without APP_PASSWORD.")
        print("Set APP_PASSWORD or use HOST=127.0.0.1.")
        return 1

    Handler.chat_server = ChatServer(app_password=app_password)
    server = ThreadingHTTPServer((host, port), Handler)

    print(f"Chat app running on http://{host}:{port}")
    storage_backend = secret_backend_name()
    if storage_backend == SECRET_FILE_BACKEND:
        print(
            "API Key is stored in an owner-only (0600) local file because no system credential "
            "helper (Keychain or secret-tool) was found."
        )
    else:
        print(f"API Key storage backend: {storage_backend}.")
    if relay_debug_enabled():
        print("Relay response debugging enabled; status, redacted headers, and raw parsed SSE chunks will be logged.")
    if is_loopback:
        print("Local-only mode. Set HOST=0.0.0.0 and APP_PASSWORD for phone access.")
    else:
        print("LAN mode enabled with password protection.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
