"""Exact-cycle tXXD readiness with old_ready left retimable."""
from migen import Module, Signal, If, Mux


class RetimedTXXD(Module):
    def __init__(self, txxd):
        self.valid = Signal()
        self.ready = Signal(reset=txxd is None)
        if txxd is None:
            self.comb += self.ready.eq(1)
            return
        event = Signal()
        count = Signal(max=max(txxd, 2))
        old_ready = Signal()
        expiry = Signal()
        bootstrap = Signal(reset=1)
        for signal in (event, count, expiry, bootstrap):
            signal.attr.add('no_retiming')
        old_ready.attr.add('preserve_syn_only')
        event.attr.add('preserve')
        # The state trails the original timer by one edge. Predict its next
        # ready bit using the captured event, without the live event cone.
        self.comb += self.ready.eq(Mux(event, int(txxd == 1),
            old_ready | expiry))
        self.sync += [event.eq(self.valid),
            If(bootstrap, bootstrap.eq(0)).Else(
                If(event,
                    count.eq(txxd - 1), old_ready.eq(int(txxd == 1)),
                    expiry.eq(int(txxd - 1 == 1))
                ).Elif(~old_ready,
                    count.eq(count - 1), expiry.eq(count == (2 % (1 << len(count)))),
                    If(count == 1, old_ready.eq(1))))]
