"""Cycle-equivalent registered refresh expiration with a cached terminal count."""
from migen import *
from migen.fhdl.bitcontainer import bits_for


class RefreshTimer2933(Module):
    def __init__(self, trefi):
        assert trefi >= 1
        self.wait = Signal()
        self.done = Signal(reset=int(trefi == 1))
        self.count = Signal(bits_for(trefi), reset=trefi - 1)
        self.terminal_one = terminal_one = Signal(reset=int(trefi == 2))
        terminal_one.attr.add("preserve_syn_only")
        decrement = self.wait & ~self.done
        # terminal_one describes the current count, captured from its exact
        # next value. It removes the wide equality from the done data cone
        # without shifting expiration or reload by an edge.
        self.sync += If(decrement,
            self.count.eq(self.count - 1),
            terminal_one.eq(self.count == 2),
            self.done.eq(terminal_one)
        ).Else(
            self.count.eq(self.count.reset),
            terminal_one.eq(int(trefi == 2)),
            self.done.eq(int(trefi == 1))
        )
