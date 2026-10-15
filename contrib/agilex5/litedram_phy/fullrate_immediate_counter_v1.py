"""Immediate-increment counter with nibble-sized registered carry prefixes."""
from migen import *


class ImmediateEventCounter(Module):
    """Count ``event`` on the same edge it is sampled; ``clear`` has priority.

    Widths must be positive multiples of four. ``prefix[j]`` means all bits
    below nibble ``j`` are one in the current count. Its next value is
    computed from the pre-increment count, so the increment datapath uses
    registered prefix flags rather than a wide carry chain.
    """
    def __init__(self, width=32, reset=0):
        assert width > 0 and width % 4 == 0
        assert 0 <= reset < (1 << width)
        self.event = Signal()
        self.clear = Signal()
        self.value = Signal(width, reset=reset)

        count = width // 4
        chunks = [Signal(4, reset=(reset >> (4*i)) & 0xf)
                  for i in range(count)]
        terminal = [Signal(reset=((reset >> (4*i)) & 0xf) == 0xf)
                    for i in range(count)]
        for flag in terminal:
            flag.attr.add("no_retiming")
        prefix = [None] + [Signal(reset=(reset & ((1 << (4*j)) - 1)) ==
                                   ((1 << (4*j)) - 1))
                           for j in range(1, count)]
        for flag in prefix[1:]:
            flag.attr.add("no_retiming")

        self.comb += self.value.eq(Cat(*chunks))
        for i, (chunk, flag) in enumerate(zip(chunks, terminal)):
            enable = self.event if i == 0 else (self.event & prefix[i])
            self.sync += If(self.clear,
                chunk.eq(0), flag.eq(0)
            ).Elif(enable,
                chunk.eq(chunk + 1), flag.eq(chunk == 0xe)
            )

        for j in range(1, count):
            lower_was_carrying = chunks[0] == 0xe
            for i in range(1, j):
                lower_was_carrying = lower_was_carrying & terminal[i]
            self.sync += If(self.clear, prefix[j].eq(0)).Elif(
                self.event, prefix[j].eq(lower_was_carrying))
