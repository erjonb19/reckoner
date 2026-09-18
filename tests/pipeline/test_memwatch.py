"""Catching a spike that falls between two log lines.

Twice a container was OOM-killed having last reported a comfortable figure --
7,140 MiB of 8,192 once, 4,284 another time -- because RSS was only written out
when a slice finished and the fatal moment was in between. The kernel's
high-water mark is correct and cannot say *when*, and a number sampled once per
slice cannot see inside one.

The window is the point: reset it per slice and a process-wide high-water
becomes a per-slice one, which is what attributes a spike to the work that
caused it.
"""

from __future__ import annotations

import time

from pipeline.memwatch import MemoryWatch


class TestTheWindow:
    def test_it_keeps_the_largest_sample(self):
        watch = MemoryWatch(sampler=lambda: 0)

        for value in (100, 4000, 250):
            watch.observe(value)

        assert watch.peak == 4000
        assert watch.window_peak == 4000

    def test_reset_returns_the_window_and_starts_a_new_one(self):
        """This is what makes a per-slice figure possible."""
        watch = MemoryWatch(sampler=lambda: 0)
        watch.observe(100)
        watch.observe(7140)

        first = watch.reset()
        watch.observe(300)

        assert first == 7140
        assert watch.window_peak == 300, "the new window must not inherit the old peak"

    def test_the_process_peak_survives_a_reset(self):
        """A spike must stay visible in the run total after its slice ends."""
        watch = MemoryWatch(sampler=lambda: 0)
        watch.observe(7140)
        watch.reset()
        watch.observe(300)

        assert watch.peak == 7140

    def test_an_unavailable_sampler_records_nothing_rather_than_zero(self):
        """Zero would read as a quiet process; None reads as no measurement."""
        watch = MemoryWatch(sampler=lambda: None)

        watch.observe(None)

        assert watch.peak is None
        assert watch.window_peak is None
        assert watch.samples == 0

    def test_availability_is_checked_rather_than_assumed(self):
        assert MemoryWatch(sampler=lambda: 512).available is True
        assert MemoryWatch(sampler=lambda: None).available is False


class TestTheThread:
    def test_it_samples_while_work_happens(self):
        """The spike here is only visible to something sampling continuously."""
        values = iter([100, 200, 9000, 150, 150, 150, 150, 150])

        def sampler() -> int:
            try:
                return next(values)
            except StopIteration:
                return 150

        with MemoryWatch(sampler=sampler, interval=0.01) as watch:
            time.sleep(0.2)

        assert watch.samples > 3, "the thread did not sample"
        assert watch.peak == 9000, "a spike between two slices must still be caught"

    def test_stopping_twice_is_harmless(self):
        watch = MemoryWatch(sampler=lambda: 10, interval=0.01).start()
        watch.stop()

        assert watch.stop() == watch.peak

    def test_a_started_watch_is_not_restarted(self):
        watch = MemoryWatch(sampler=lambda: 10, interval=0.01)

        assert watch.start() is watch.start()
        watch.stop()
