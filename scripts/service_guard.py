#!/usr/bin/env python3
"""Palimpsest service guard — restart the local services when they die.

Guards two services that Palimpsest depends on:

  * Palimpsest REST ``:8090``  — started via ``scripts/start_rest.vbs`` (Windows)
    or the configured start command (other platforms).
  * Ollama ``:11434``          — the embedding backend Palimpsest depends on.

Why this exists
---------------
Both services can die silently. If the REST service dies, every memory tool
fails. If Ollama dies, semantic search quietly degrades to keyword matching --
no error is raised and the degradation is easy to miss.

Why restarts must be serialised (important)
-------------------------------------------
TriviumDB opens the database file in **exclusive** mode: while one process
holds the database, a second one cannot even open it read-only. Palimpsest
uses a "open-close the database per operation" pattern, so when two REST
processes run at the same time they fight over the file. A write that loses
that race is interrupted mid-flight, which leaves the storage generation
inconsistent (``.flush_ok`` no longer matches ``.vec``/``.pld``) and the
database degrades from read-write to unreadable.

A naive watchdog causes exactly this: an HTTP liveness probe treats a service
that is *still starting* (loading the embedding model, building indexes) as
dead, so it spawns a second process while the first is still running.

This guard therefore checks, before every start attempt:
  1. whether something is already listening on the service port, and
  2. whether the previous start is still in its startup grace period,
and refuses to spawn a duplicate.

Output contract (for cron / scheduler integration):
  * everything healthy   -> exit 0, empty stdout  (silence = healthy)
  * a service was started -> one line on stdout
  * stdout stays silent otherwise; every start attempt is appended to the
    event log for external health reporting.

Paths are never hard-coded: the Palimpsest root comes from the
``PALIMPSEST_HOME`` environment variable (or a resolved ``.env``), so moving
the checkout only requires changing that one value.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env-driven; no hard-coded machine paths)
# ---------------------------------------------------------------------------

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
PS_HOME = os.getenv("PALIMPSEST_HOME") or ""

REST_HOST = os.getenv("PALIMPSEST_REST_HOST", "127.0.0.1")
REST_PORT = int(os.getenv("PALIMPSEST_REST_PORT", "8090"))
OLLAMA_HOST = os.getenv("OLLAMA_HOST_URL", "127.0.0.1")
OLLAMA_PORT = int(os.getenv("OLLAMA_PORT", "11434"))

REST_URL = f"http://{REST_HOST}:{REST_PORT}/"
OLLAMA_URL = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/tags"

EVENTS = os.path.join(HERMES_HOME, "logs", "guard_events.jsonl")

# How long a freshly started service is given before it is considered dead.
# Palimpsest needs to load the embedding model and build indexes on startup;
# this window prevents the guard from treating "still starting" as "dead".
STARTUP_GRACE_SECONDS = float(os.getenv("PALIMPSEST_GUARD_STARTUP_GRACE", "90"))

# Marker directory: one file per service, holding the unix time of the last
# start attempt. A start that is still inside the grace window blocks another.
_STATE_DIR = os.path.join(HERMES_HOME, "logs", "guard_state")

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


def _http_alive(url: str, timeout: int = 4) -> bool:
    """HTTP liveness probe. ``True`` when the service answers below 500."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:  # noqa: BLE001 —— 探测失败即「不健康」，不区分异常类型
        return False


def _port_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """Whether *anything* is listening on host:port.

    This is the check that a plain HTTP probe cannot give: a socket that
    accepts connections proves a process owns the port, even if it has not
    finished starting up and cannot answer an HTTP request yet.

    The probe socket is closed immediately and the result is decided purely
    by whether the TCP handshake completed. This matters because a process
    that is *listening but not yet accepting* keeps half-open connections in
    its backlog; leaving our probe socket open would occupy a backlog slot
    and make a **later** probe see the port as unreachable -- a
    false-negative that would itself trigger a duplicate start.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _service_up(url: str, host: str, port: int) -> bool:
    """A service counts as up if it either holds its port or answers HTTP.

    Port check comes **first** and is deliberately cheap: it returns as soon
    as the TCP handshake completes. The HTTP probe is the expensive one (it
    can block for its full timeout against a process that is listening but
    not yet serving), and running it first would occupy the listening
    socket's backlog and make the port probe itself time out -- which is
    exactly the false-negative that leads to a duplicate start.

    Ordering also matches causality: nothing listening on the port means no
    HTTP answer is possible, so there is no point probing HTTP first.
    """
    if _port_listening(host, port):
        return True
    return _http_alive(url)


# ---------------------------------------------------------------------------
# Start bookkeeping (prevents overlapping starts)
# ---------------------------------------------------------------------------


def _state_path(service: str) -> str:
    return os.path.join(_STATE_DIR, f"{service}.last_start")


def _record_start(service: str) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_state_path(service), "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except Exception as e:  # noqa: BLE001 —— 记录启动时刻失败不应阻塞守护主流程
        logger.debug("guard: failed to record start for %s: %s", service, e)


def _within_grace(service: str) -> bool:
    """True when the last start attempt for *service* is still in its grace window."""
    try:
        with open(_state_path(service), encoding="utf-8") as fh:
            started = float(fh.read().strip())
    except Exception:  # noqa: BLE001 —— 状态文件缺失/损坏一律视为不在宽限期
        return False
    return (time.time() - started) < STARTUP_GRACE_SECONDS


def _record_event(kind: str, detail: str) -> None:
    """Append one guard event; never let logging break the guard."""
    try:
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "detail": detail,
        }
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: S110, BLE001 —— 事件日志失败绝不能拖垮守护本身
        pass


# ---------------------------------------------------------------------------
# Starters
# ---------------------------------------------------------------------------


def _resolve_ps_home() -> str:
    """Resolve the Palimpsest checkout: env var first, then ``~/.hermes/.env``."""
    if PS_HOME:
        return PS_HOME
    env_file = os.path.join(HERMES_HOME, ".env")
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("PALIMPSEST_HOME="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:  # noqa: S110, BLE001 —— .env 不可读时退回默认查找，不影响守护
        pass
    return ""


def _windows_start_script(ps_home: str) -> str | None:
    candidate = os.path.join(ps_home, "scripts", "start_rest.vbs")
    return candidate if os.path.isfile(candidate) else None


def _start_rest(ps_home: str) -> bool:
    """Start the REST service.

    Windows: run the shipped ``start_rest.vbs`` through cscript.
    Other platforms: run uvicorn directly from the checkout.
    """
    if not ps_home or not os.path.isdir(ps_home):
        return False
    try:
        if os.name == "nt":
            vbs = _windows_start_script(ps_home)
            if not vbs:
                return False
            subprocess.Popen(
                ["cscript", "//nologo", vbs],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            python = os.path.join(ps_home, "venv", "bin", "python")
            if not os.path.isfile(python):
                python = sys.executable
            subprocess.Popen(
                [
                    python,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    REST_HOST,
                    "--port",
                    str(REST_PORT),
                    "--log-level",
                    "warning",
                ],
                cwd=ps_home,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except Exception:  # noqa: BLE001 —— 拉起失败返回 False，由调用方记录事件
        return False


def _start_ollama() -> bool:
    """Start Ollama. Uses the ``ollama`` executable found on PATH."""
    import shutil as _shutil

    exe = _shutil.which("ollama")
    if not exe:
        # Fall back to the default Windows install location.
        guess = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        exe = guess if os.path.isfile(guess) else None
    if not exe:
        return False
    try:
        env = os.environ.copy()
        # Drop OLLAMA_ORIGINS: Values like app://obsidian.md are rejected by
        # newer Ollama releases and make the server crash on start.
        env.pop("OLLAMA_ORIGINS", None)
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW | _DETACHED_PROCESS
        subprocess.Popen([exe, "serve"], **kwargs)
        return True
    except Exception:  # noqa: BLE001 —— 拉起失败返回 False，由调用方记录事件
        return False


# ---------------------------------------------------------------------------
# Guard loop
# ---------------------------------------------------------------------------


def _wait_alive(url: str, attempts: int, interval: float = 2.0) -> bool:
    for _ in range(attempts):
        time.sleep(interval)
        if _http_alive(url):
            return True
    return False


def main() -> int:
    messages: list[str] = []
    ps_home = _resolve_ps_home()

    # --- Palimpsest REST ---------------------------------------------------
    if not _service_up(REST_URL, REST_HOST, REST_PORT):
        if _within_grace("rest"):
            # A start attempt is still inside its grace window; spawning
            # another process here is exactly what corrupts the database.
            pass
        else:
            _record_start("rest")
            issued = _start_rest(ps_home)
            back = _wait_alive(REST_URL, 20) if issued else False
            if back:
                messages.append(f"Palimpsest REST :{REST_PORT} restarted")
                _record_event("rest", "restarted")
            else:
                messages.append(f"Palimpsest REST :{REST_PORT} start failed")
                _record_event("rest", "restart_failed")

    # --- Ollama ------------------------------------------------------------
    if not _service_up(OLLAMA_URL, OLLAMA_HOST, OLLAMA_PORT):
        if _within_grace("ollama"):
            pass
        else:
            _record_start("ollama")
            issued = _start_ollama()
            back = _wait_alive(OLLAMA_URL, 15) if issued else False
            if back:
                messages.append(f"Ollama :{OLLAMA_PORT} restarted")
                _record_event("ollama", "restarted")
            else:
                messages.append(f"Ollama :{OLLAMA_PORT} start failed")
                _record_event("ollama", "restart_failed")

    if messages:
        print("[guard] " + "; ".join(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
