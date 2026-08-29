"""Lifecycle for the loopback-only X11/VNC desktop in a browser workspace."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from devai.sandbox.workspace import BROWSER_PORT

_DISPLAY = ":99"


def desktop_commands() -> list[list[str]]:
    return [
        ["Xvfb", _DISPLAY, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
        ["fluxbox", "-display", _DISPLAY],
        ["x11vnc", "-display", _DISPLAY, "-rfbport", "5900", "-localhost", "-forever", "-shared", "-nopw"],
        [
            "websockify",
            "--web=/usr/share/novnc",
            f"127.0.0.1:{BROWSER_PORT}",
            "127.0.0.1:5900",
        ],
    ]


class BrowserDesktop:
    def __init__(self) -> None:
        self._processes: list[subprocess.Popen[Any]] = []

    def start(self) -> None:
        env = {**os.environ, "DISPLAY": _DISPLAY}
        commands = desktop_commands()
        self._processes.append(
            subprocess.Popen(commands[0], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        )
        socket_path = Path("/tmp/.X11-unix/X99")
        deadline = time.monotonic() + 5
        while not socket_path.exists():
            if self._processes[0].poll() is not None or time.monotonic() >= deadline:
                self.stop()
                raise RuntimeError("browser desktop display failed to start")
            time.sleep(0.05)
        for command in commands[1:]:
            self._processes.append(
                subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            )

    def stop(self) -> None:
        for process in reversed(self._processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(self._processes):
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._processes.clear()

    def __enter__(self) -> BrowserDesktop:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()


__all__ = ["BrowserDesktop", "desktop_commands"]
