"""Runtime settings, read from the environment (and `.env` for local runs).

Spec section 6 lists the variables. Each process builds one `Settings` at startup, so a missing or
malformed value fails fast at boot, not in the middle of an ingest.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://gateway:gateway@postgres:5432/gateway"
    db_pool_size: int = Field(default=10, ge=1)  # API only; the poller uses its own connection

    # --- SFTP drop (FR-1) ---
    sftp_host: str = "sftp"
    sftp_port: int = 22
    sftp_user: str = "gateway"
    sftp_remote_dir: str = "/outbound/shipments"
    sftp_key_path: Path = Path("/run/secrets/gateway_ed25519")
    sftp_known_hosts: Path = Path("/run/secrets/known_hosts")
    # The name the host key is pinned under. Unset inside Compose (we connect to "sftp" already);
    # set to "sftp" when running from the host against localhost:2222, like `ssh -o HostKeyAlias`.
    sftp_host_key_alias: str | None = None
    sftp_poll_interval_s: float = Field(default=60, gt=0)

    # --- SOAP rate-quote service (FR-4, ADR-P01-1) ---
    soap_base_url: str = "http://soap-mock:8080"
    # Bulkhead size: Meridian allows 5 concurrent calls; we take 4, leaving 1 for their own callers.
    soap_max_concurrency: int = Field(default=4, ge=1)
    soap_bulkhead_wait_s: float = Field(default=0.2, gt=0)  # spec M5: wait_for(..., timeout=0.2)
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_reset_after_s: float = Field(default=30.0, gt=0)

    # --- Webhooks (M7) ---
    # Retry horizon before dead-lettering: "72h" in production, "120s" for the M7 demo.
    webhook_max_age: str = Field(default="72h", pattern=r"^[0-9]+[smhd]$")
    webhook_timeout_s: float = Field(default=5.0, gt=0)  # total per attempt (spec: 5 s)
    webhook_base_delay_s: float = Field(default=30.0, gt=0)  # full jitter from 30 s...
    webhook_max_delay_s: float = Field(default=6 * 3600, gt=0)  # ...doubling to a 6 h cap
    webhook_poll_interval_s: float = Field(default=1.0, gt=0)
    webhook_batch_size: int = Field(default=50, ge=1)
    # Lab only: exact hostnames that skip the SSRF address check (the local sink); never in prod.
    webhook_dev_allow_hosts: str = ""
    # Lab only: CA bundle the DISPATCHER trusts for the sink's self-signed certificate.
    webhook_ca_bundle: Path | None = None

    @property
    def webhook_max_age_s(self) -> float:
        n, unit = int(self.webhook_max_age[:-1]), self.webhook_max_age[-1]
        return float(n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])

    @property
    def dev_allow_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.webhook_dev_allow_hosts.split(",") if h.strip()
        )

    # --- Ingest ---
    ingest_batch_size: int = Field(default=1000, ge=1)
    migrations_dir: Path = Path("migrations")


def get_settings() -> Settings:
    return Settings()
