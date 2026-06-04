"""Long-running Horizon daemon for containers.

The daemon runs the Telegram callback HTTP service and a background Horizon
scheduler in the same process. It is intended for Docker/Kubernetes-style
deployments where the container should stay alive instead of exiting after one
daily run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from rich.console import Console

from ..models import Config, DaemonConfig, TelegramBotConfig
from ..orchestrator import HorizonOrchestrator
from ..storage.manager import ConfigError, StorageManager
from .telegram_bot import create_app


console = Console()


class HorizonDaemonScheduler:
    """Background scheduler that periodically runs the Horizon pipeline."""

    def __init__(
        self,
        *,
        storage: StorageManager,
        daemon_config: DaemonConfig,
    ):
        self.storage = storage
        self.daemon_config = daemon_config
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._run_lock = asyncio.Lock()

    def start(self) -> None:
        if not self.daemon_config.enabled:
            console.print("[yellow]Daemon scheduler is disabled.[/yellow]")
            return
        self._task = asyncio.create_task(self._loop(), name="horizon-daemon-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        cfg = self.daemon_config

        if cfg.startup_delay_sec > 0:
            await self._sleep_or_stop(cfg.startup_delay_sec)
            if self._stop_event.is_set():
                return

        if cfg.run_on_startup:
            await self.run_once(reason="startup")

        while not self._stop_event.is_set():
            delay = self._next_delay_seconds()
            next_run = datetime.now().astimezone() + timedelta(seconds=delay)
            console.print(
                f"[cyan]Next Horizon daemon run in {delay:.0f}s "
                f"at {next_run.isoformat(timespec='seconds')}[/cyan]"
            )
            await self._sleep_or_stop(delay)
            if self._stop_event.is_set():
                return
            await self.run_once(reason="scheduled")

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(delay, 0))
        except asyncio.TimeoutError:
            return

    async def run_once(self, *, reason: str) -> None:
        if self._run_lock.locked():
            console.print(
                f"[yellow]Skipping {reason} run because a Horizon run is still active.[/yellow]"
            )
            return

        async with self._run_lock:
            started_at = datetime.now().astimezone()
            console.print(
                f"[bold cyan]Starting Horizon daemon {reason} run at "
                f"{started_at.isoformat(timespec='seconds')}[/bold cyan]"
            )
            try:
                # Reload config each run so source/filter/token changes under
                # the mounted data/ directory are picked up without rebuilding.
                config = self.storage.load_config()
                orchestrator = HorizonOrchestrator(config, self.storage)
                force_hours = (
                    config.daemon.force_hours
                    if config.daemon and config.daemon.force_hours
                    else self.daemon_config.force_hours
                )
                await orchestrator.run(force_hours=force_hours)
            except Exception as exc:
                console.print(
                    f"[bold red]Horizon daemon {reason} run failed: {exc}[/bold red]"
                )
            else:
                finished_at = datetime.now().astimezone()
                console.print(
                    f"[bold green]Horizon daemon {reason} run finished at "
                    f"{finished_at.isoformat(timespec='seconds')}[/bold green]"
                )

    def _next_delay_seconds(self) -> float:
        cfg = self.daemon_config
        if cfg.mode == "interval":
            return cfg.interval_hours * 3600

        zone = self._zoneinfo(cfg.timezone)
        now = datetime.now(zone)
        hour_str, minute_str = cfg.time.split(":", 1)
        target_time = dt_time(hour=int(hour_str), minute=int(minute_str), tzinfo=zone)
        target = datetime.combine(now.date(), target_time)
        if target <= now:
            target += timedelta(days=1)
        return max((target - now).total_seconds(), 0)

    @staticmethod
    def _zoneinfo(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            console.print(
                f"[yellow]Unknown daemon.timezone '{name}', falling back to UTC.[/yellow]"
            )
            return ZoneInfo("UTC")


def _load_config(data_dir: str = "data") -> tuple[StorageManager, Config]:
    storage = StorageManager(data_dir=data_dir)
    try:
        return storage, storage.load_config()
    except FileNotFoundError as exc:
        raise SystemExit("Configuration file not found. Create data/config.json.") from exc
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc


def create_daemon_app(config: Config, storage: StorageManager):
    """Create the ASGI app that hosts Telegram callbacks and the scheduler."""
    telegram_config = config.telegram_bot or TelegramBotConfig()
    daemon_config = config.daemon or DaemonConfig()
    scheduler = HorizonDaemonScheduler(
        storage=storage,
        daemon_config=daemon_config,
    )

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    return create_app(
        telegram_config,
        data_dir=storage.data_dir,
        lifespan=lifespan,
    )


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _should_run_http_server(telegram_config: TelegramBotConfig) -> bool:
    """Only local Telegram webhook mode needs an HTTP listener."""
    return telegram_config.enabled and not _env(telegram_config.worker_url_env)


async def run_scheduler_only(config: Config, storage: StorageManager) -> None:
    """Run the daemon scheduler without opening an HTTP listener."""
    scheduler = HorizonDaemonScheduler(
        storage=storage,
        daemon_config=config.daemon or DaemonConfig(),
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    console.print("[cyan]Running Horizon daemon scheduler without HTTP listener.[/cyan]")
    scheduler.start()
    try:
        await stop_event.wait()
    finally:
        await scheduler.stop()


def main() -> None:
    """CLI entry point for container-friendly long-running service."""
    parser = argparse.ArgumentParser(description="Run Horizon as a long-running daemon")
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory containing config.json and runtime state.",
    )
    args = parser.parse_args()

    load_dotenv()
    storage, config = _load_config(data_dir=args.data_dir)
    telegram_config = config.telegram_bot or TelegramBotConfig()

    if not _should_run_http_server(telegram_config):
        asyncio.run(run_scheduler_only(config, storage))
        return

    import uvicorn

    uvicorn.run(
        create_daemon_app(config, storage),
        host=telegram_config.host,
        port=telegram_config.port,
        proxy_headers=telegram_config.proxy_headers,
        forwarded_allow_ips=telegram_config.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
