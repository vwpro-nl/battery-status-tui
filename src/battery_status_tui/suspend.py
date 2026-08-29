"""Suspend/resume detection from clocks, logind and the system journal."""
from __future__ import annotations
import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from .models import RawBatterySnapshot, SleepInterval

SLEEP_TOLERANCE_SECONDS = 5.0


class PrepareForSleepParser:
    """Parse only the boolean body belonging to a PrepareForSleep signal."""
    def __init__(self) -> None:
        self._member = False

    def feed(self, line: str) -> bool | None:
        stripped = line.strip()
        if stripped.startswith("‣ Type=") or stripped.startswith("Type="):
            self._member = False
        if "Member=PrepareForSleep" in stripped:
            self._member = True
            return None
        if not self._member:
            return None
        match = re.fullmatch(r"(?:b|BOOLEAN)\s+(true|false);?", stripped, re.IGNORECASE)
        if match is None:
            return None
        self._member = False
        return match.group(1).lower() == "true"

def clock_sleep(previous: RawBatterySnapshot, current: RawBatterySnapshot) -> SleepInterval | None:
    if previous.boot_id != current.boot_id:
        return None
    suspended = (current.boottime_s - previous.boottime_s) - (current.monotonic_s - previous.monotonic_s)
    if suspended <= SLEEP_TOLERANCE_SECONDS:
        return None
    end = current.timestamp
    return SleepInterval(round(end - suspended), end, source="clocks", boot_id=current.boot_id,
                         pre_percentage=previous.percentage, post_percentage=current.percentage)

def parse_journal(lines: str) -> list[SleepInterval]:
    pending: tuple[int, str | None, str] | None = None
    intervals: list[SleepInterval] = []
    for line in lines.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = str(item.get("MESSAGE", ""))
        timestamp = int(item.get("__REALTIME_TIMESTAMP", 0)) // 1_000_000
        boot_id = item.get("_BOOT_ID")
        if (pending and boot_id and pending[1] and boot_id != pending[1]
                and timestamp > pending[0]):
            intervals.append(SleepInterval(
                pending[0], timestamp, pending[2], "journal", pending[1]
            ))
            pending = None
        if ("PM: suspend entry" in message
                or "PM: hibernation: hibernation entry" in message):
            kind = "hibernate" if "hibernation" in message.lower() else "suspend"
            pending = (timestamp, boot_id, kind)
        elif (("PM: suspend exit" in message
               or "PM: hibernation: hibernation exit" in message)
              and pending and timestamp > pending[0]):
            intervals.append(SleepInterval(pending[0], timestamp, pending[2], "journal", pending[1]))
            pending = None
    return intervals

def journal_intervals(since: int, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> list[SleepInterval]:
    try:
        result = runner(["journalctl", "-b", "all", "-k", "-o", "json", "--since", f"@{since}"],
                        text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_journal(result.stdout) if result.returncode == 0 else []

class LogindMonitor:
    """Dependency-free listener for logind PrepareForSleep signals."""
    def __init__(self, popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen):
        self.popen = popen
        self.events: queue.Queue[tuple[bool, int]] = queue.Queue()
        self.wakeup = threading.Event()
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self._process = self.popen(["busctl", "monitor", "org.freedesktop.login1"], text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            parser = PrepareForSleepParser()
            assert self._process.stdout is not None
            for line in self._process.stdout:
                sleeping = parser.feed(line)
                if sleeping is not None:
                    self.events.put((sleeping, int(time.time())))
                    self.wakeup.set()
        except (OSError, subprocess.SubprocessError):
            return

    def drain(self) -> list[tuple[bool, int]]:
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                break
        self.wakeup.clear()
        return events

    def close(self) -> None:
        if self._process is not None:
            self._process.terminate()
