"""Sample resident memory continuously, so a spike between log lines is visible.

``resource.getrusage`` reports the kernel's high-water mark for the whole
process. It is correct and it is not enough: it never falls, so it cannot say
*when* the peak happened, and the job only wrote it out when a slice finished.
Twice a container was killed having last reported a comfortable figure --
7,140 MiB of 8,192 in one case, 4,284 in another -- because the fatal moment fell
between two samples. Absence of a high reading is not absence of a high moment.

So a thread reads current RSS every second and keeps the largest value it has
seen since it was last reset. Resetting per slice turns one process-wide number
into a per-slice one, which is what attributes a spike to the work that caused
it.

The sampler is injectable and the Linux one is a one-line read of
``/proc/self/status``. No dependency: psutil would be a wheel in the image for
something the kernel already exposes as a text file.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

#: How often to sample. A second is far finer than the seconds-to-minutes a
#: slice takes, and costs one small file read.
INTERVAL_SECONDS = 1.0

STATUS = Path("/proc/self/status")


def linux_rss_mib() -> int | None:
    """Current resident set size, or ``None`` where ``/proc`` is not a thing.

    Returns ``None`` on Windows rather than raising, so the same code runs on a
    laptop and reports honestly that it cannot see the number.
    """
    try:
        for line in STATUS.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


class MemoryWatch:
    """A background sampler of resident memory, with a resettable window.

    Two numbers are kept. ``peak`` is the largest ever seen, which is the
    process high-water. ``window_peak`` is the largest since :meth:`reset`,
    which is what makes a per-slice figure possible.
    """

    def __init__(
        self,
        sampler: Callable[[], int | None] = linux_rss_mib,
        interval: float = INTERVAL_SECONDS,
    ) -> None:
        self._sampler = sampler
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak: int | None = None
        self.window_peak: int | None = None
        self.samples = 0

    @property
    def available(self) -> bool:
        """Whether the sampler can see anything at all.

        Checked once up front so a run on a platform without ``/proc`` says so,
        rather than reporting ``None`` peaks that look like a quiet process.
        """
        return self._sampler() is not None

    def observe(self, value: int | None) -> None:
        """Fold one sample in. Separate from the thread so it can be tested."""
        if value is None:
            return
        with self._lock:
            self.samples += 1
            self.peak = value if self.peak is None else max(self.peak, value)
            self.window_peak = value if self.window_peak is None else max(self.window_peak, value)

    def reset(self) -> int | None:
        """Close the window and start a new one, returning what it held."""
        with self._lock:
            held = self.window_peak
            self.window_peak = None
        return held

    def start(self) -> MemoryWatch:
        if self._thread is not None:
            return self
        # A daemon thread, so a crashing stage cannot be held open by its own
        # instrumentation. Losing the last second of samples in that case is
        # the right trade.
        self._thread = threading.Thread(target=self._run, name="memwatch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> int | None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._interval * 2)
        return self.peak

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.observe(self._sampler())

    def __enter__(self) -> MemoryWatch:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["INTERVAL_SECONDS", "MemoryWatch", "linux_rss_mib"]
