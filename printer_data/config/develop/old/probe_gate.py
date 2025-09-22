
# "Call only if not probing" gate.
# - Busy = probe.probe_session.hw_probe_session
# - call(fn, *args, **kwargs): runs now if not busy, else queues
# - After busy clears, waits settle_time, then runs queued calls.

class ProbeGate:
    def __init__(self, printer, settle_time=2.0, echo=False):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self._settle = float(settle_time)
        self._echo = bool(echo)

        self._probe_session = None
        self._queue = []              # list[(fn, args, kwargs, name)]
        self._timer = None
        self._settle_deadline = 0.0

        self.printer.register_event_handler('klippy:connect', self._on_connect)

    # ---- public API -------------------------------------------------------

    def call(self, fn, *args, **kwargs):
        """Run fn(...) now if not probing; else queue it to run after settle."""
        if not callable(fn):
            raise TypeError("ProbeGate.call: fn must be callable")

        if not self._busy() and self._settle_deadline == 0.0:
            return self._run(fn, args, kwargs)

        self._queue.append((fn, args, kwargs, getattr(fn, "__name__", "fn")))
        self._arm_timer()
        return None  # nothing to return until it actually runs

    def is_busy(self):
        """True if probing (or we’re in the settle window)."""
        return self._busy() or self._settle_deadline > 0.0

    def pending_count(self):
        return len(self._queue)

    def clear(self):
        """Drop all queued calls."""
        self._queue.clear()
        self._cancel_timer()
        self._settle_deadline = 0.0

    # ---- init / internals -------------------------------------------------

    def _on_connect(self):
        prb = self.printer.lookup_object('probe', None)
        self._probe_session = prb.probe_session if prb is not None else None

    def _busy(self):
        ps = self._probe_session
        return bool(ps and getattr(ps, 'hw_probe_session', None))

    def _arm_timer(self):
        if self._timer is None:
            self._timer = self.reactor.register_timer(self._timer_cb)
        self.reactor.update_timer(self._timer, self.reactor.monotonic() + 0.05)

    def _cancel_timer(self):
        if self._timer is not None:
            self.reactor.update_timer(self._timer, self.reactor.NEVER)
            self._timer = None

    def _timer_cb(self, eventtime):
        # Still probing: reset settle and keep waiting.
        if self._busy():
            self._settle_deadline = 0.0
            return eventtime + 0.05

        # Just became idle: start settle window.
        if self._settle_deadline == 0.0:
            self._settle_deadline = eventtime + self._settle
            return eventtime + 0.05

        # Settling.
        if eventtime < self._settle_deadline:
            return eventtime + 0.05

        # Settled: drain the queue (run all; they asked for it, they get it).
        q, self._queue = self._queue, []
        self._settle_deadline = 0.0
        self._cancel_timer()

        for fn, args, kwargs, name in q:
            self._run(fn, args, kwargs, name)

        return self.reactor.NEVER

    def _run(self, fn, args, kwargs, name=None):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if self._echo:
                g = self.printer.lookup_object('gcode')
                g.respond_info(f"ProbeGate: call '{name or fn}' raised: {e}")
            # Don’t kill the reactor timer chain.
            return None
