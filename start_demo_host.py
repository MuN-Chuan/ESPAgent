from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "apps" / "demo_host"


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the ESPAgent demo host.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--status-port", type=int, default=int(os.environ.get("ESP32_STATUS_PORT", "39001")))
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args()

    kill_port_owners("tcp", args.port)
    kill_port_owners("udp", args.status_port)
    set_high_priority()

    if args.log_file is not None:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        log_stream = args.log_file.open("a", encoding="utf-8", buffering=1)
        sys.stdout = log_stream
        sys.stderr = log_stream

    sys.path.insert(0, str(APP_DIR))
    import uvicorn

    print(f"Starting ESPAgent demo host at http://{args.host}:{args.port}")
    uvicorn.run(
        "espagent_demo.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT)] if args.reload else None,
        app_dir=str(APP_DIR),
        access_log=False,
    )


def set_high_priority() -> None:
    if os.name != "nt":
        return
    command = f"$p = Get-Process -Id {os.getpid()}; $p.PriorityClass = 'High'"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        **hidden_subprocess_options(),
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        print(f"Warning: failed to raise process priority: {message}", file=sys.stderr)


def kill_port_owners(protocol: str, port: int) -> list[int]:
    pids = port_owner_pids(protocol, port)
    killed: list[int] = []
    current_pid = os.getpid()
    for pid in pids:
        if pid <= 0 or pid == current_pid:
            continue
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
            **hidden_subprocess_options(),
        )
        if result.returncode == 0:
            killed.append(pid)
        else:
            message = (result.stderr or result.stdout).strip()
            print(f"Warning: failed to kill PID {pid} on {protocol.upper()} port {port}: {message}", file=sys.stderr)
    if killed:
        print(f"Killed process(es) on {protocol.upper()} port {port}: {', '.join(str(pid) for pid in killed)}")
        time.sleep(0.3)
    return killed


def port_owner_pids(protocol: str, port: int) -> list[int]:
    proto = protocol.lower()
    result = subprocess.run(
        ["netstat", "-ano", "-p", proto],
        capture_output=True,
        text=True,
        check=False,
        **hidden_subprocess_options(),
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        print(f"Warning: netstat failed for {proto.upper()} port {port}: {message}", file=sys.stderr)
        return []

    pids: list[int] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.split()
        if len(parts) < 4 or parts[0].lower() != proto:
            continue
        if proto == "tcp" and "LISTENING" not in [part.upper() for part in parts]:
            continue
        if not local_address_matches_port(parts[1], port):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def local_address_matches_port(local_address: str, port: int) -> bool:
    return local_address.rsplit(":", 1)[-1] == str(port)


def hidden_subprocess_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startup_info,
    }


if __name__ == "__main__":
    main()
