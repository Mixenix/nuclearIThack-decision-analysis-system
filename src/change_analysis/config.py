"""Configuration — the only place that reads the environment.

Nothing else in the application calls ``os.environ``.  Secrets never reach the
compute subprocess (PLAN.md §11.6): the child gets an explicit, minimal
environment built by :func:`child_env`.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def _load_dotenv(path: Path) -> dict[str, str]:
    """Tiny .env reader (no third-party dependency)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        out[key] = value
    return out


APP_ROOT = Path(__file__).resolve().parents[2]      # .../app
REPO_ROOT = APP_ROOT.parent
DEFAULT_TASK_PACKAGE = REPO_ROOT / "ХАКАТОН_СИСТЕМА_АНАЛИЗА_ИЗМЕНЕНИЙ" / "ПАКЕТ" / "ЗАДАНИЕ"


@dataclass
class Settings:
    database_url: str
    dataset_root: Path
    script_root: Path
    artifact_root: Path
    compute_python: str
    compute_backend: str = "cpu"            # cpu | gpu
    public_base_url: str = "http://127.0.0.1:8080"
    app_storage_secret: str = ""
    stage_timeout_sec: int = 1800
    heartbeat_sec: int = 5
    lease_sec: int = 120
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key_env: str = ""
    llm_base_url: str = ""
    llm_timeout_sec: int = 60
    llm_max_payload_chars: int = 24000
    llm_max_evidence: int = 40
    host: str = "127.0.0.1"
    port: int = 8080
    storage_secret: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)

    # -- derived helpers ----------------------------------------------------
    @property
    def task_package(self) -> Path:
        return Path(self.extra.get("TASK_PACKAGE", str(DEFAULT_TASK_PACKAGE)))

    @property
    def checks_dir(self) -> Path:
        return Path(self.extra.get("CHECKS_DIR", str(self.task_package / "checks")))

    def llm_available(self) -> bool:
        if not self.llm_provider or not self.llm_api_key_env:
            return False
        return bool(os.environ.get(self.llm_api_key_env, "").strip())

    def child_env(self) -> dict[str, str]:
        """Explicit minimal environment for the compute subprocess."""
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": os.environ.get("HOME", "/tmp"),
            # deliberately no DATABASE_URL, no APP_STORAGE_SECRET, no LLM keys
        }


_CACHE: Settings | None = None


def load_settings(force: bool = False) -> Settings:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    env = dict(os.environ)
    dotenv = _load_dotenv(APP_ROOT / ".env")
    for key, value in dotenv.items():
        env.setdefault(key, value)
        # The web process is a long-lived service, not a shell: values from
        # ``.env`` must be visible to ``Settings.llm_available()`` and to the
        # LLM adapter, which read ``os.environ``.  The compute subprocess gets an
        # explicit minimal environment from ``Settings.child_env()``, so secrets
        # still never reach user code (PLAN.md §11.6).
        os.environ.setdefault(key, value)

    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip() or default

    def path(name: str, default: Path) -> Path:
        raw = get(name)
        return Path(raw).expanduser().resolve() if raw else default

    secret = get("APP_STORAGE_SECRET")
    if not secret:
        secret_file = APP_ROOT / ".storage_secret"
        if secret_file.exists():
            secret = secret_file.read_text(encoding="utf-8").strip()
        if not secret:
            secret = secrets.token_urlsafe(32)
            try:
                secret_file.write_text(secret, encoding="utf-8")
                os.chmod(secret_file, 0o600)
            except OSError:
                pass

    database_url = get(
        "DATABASE_URL",
        "postgresql://ca:ca@127.0.0.1:55432/change_analysis",
    )

    settings = Settings(
        database_url=database_url,
        dataset_root=path("DATASET_ROOT", DEFAULT_TASK_PACKAGE / "data"),
        script_root=path("SCRIPT_ROOT", APP_ROOT / "src" / "change_analysis" / "procedures"),
        artifact_root=path("ARTIFACT_ROOT", APP_ROOT / "artifacts"),
        compute_python=get("COMPUTE_PYTHON", str(APP_ROOT / ".venv" / "bin" / "python")),
        compute_backend=get("COMPUTE_BACKEND", "cpu").lower(),
        public_base_url=get("PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
        app_storage_secret=secret,
        storage_secret=secret,
        stage_timeout_sec=int(get("STAGE_TIMEOUT_SEC", "1800")),
        heartbeat_sec=int(get("HEARTBEAT_SEC", "5")),
        lease_sec=int(get("LEASE_SEC", "120")),
        llm_provider=get("LLM_PROVIDER"),
        llm_model=get("LLM_MODEL"),
        llm_api_key_env=get("LLM_API_KEY_ENV"),
        llm_base_url=get("LLM_BASE_URL"),
        llm_timeout_sec=int(get("LLM_TIMEOUT_SEC", "60")),
        llm_max_payload_chars=int(get("LLM_MAX_PAYLOAD_CHARS", "24000")),
        llm_max_evidence=int(get("LLM_MAX_EVIDENCE", "40")),
        host=get("APP_HOST", "127.0.0.1"),
        port=int(get("APP_PORT", "8080")),
        extra={k: v for k, v in env.items() if k in ("TASK_PACKAGE", "CHECKS_DIR")},
    )
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    _CACHE = settings
    return settings
