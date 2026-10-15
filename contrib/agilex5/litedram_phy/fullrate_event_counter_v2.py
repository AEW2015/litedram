"""32-bit event counter using registered 4-bit carry-prefix flags."""
from migen import *


class PipelinedEventCounter(Module):
    """Count events one cycle later, matching ``fullrate_event_counter_v1``.

    ``pending`` delays each event by one cycle.  The prefix flag for nibble
    ``j`` says that all lower nibbles of the *current* value are ``0xf``;
    it lets the increment path avoid a combinational carry chain across
    multiple nibbles.  Clear also discards an event pending on that edge.
    """
    def __init__(self, reset=0):
        assert 0 <= reset < 2**32
        self.event = Signal()
        self.clear = Signal()
        self.value = Signal(32)

        pending = Signal()
        pending.attr.add("no_retiming")
        self.sync += If(self.clear, pending.eq(0)).Else(pending.eq(self.event))

        chunks = [Signal(4, reset=(reset >> (4*i)) & 0xf) for i in range(8)]
        terminal = [Signal(reset=((reset >> (4*i)) & 0xf) == 0xf)
                    for i in range(8)]
        for flag in terminal:
            flag.attr.add("no_retiming")

        # prefix[j] is true exactly when current_value[4*j-1:0] is all ones.
        prefix = [None] + [Signal(reset=(reset & ((1 << (4*j)) - 1)) ==
                                   ((1 << (4*j)) - 1)) for j in range(1, 8)]
        for flag in prefix[1:]:
            flag.attr.add("no_retiming")

        self.comb += self.value.eq(Cat(*chunks))

        for i, (chunk, flag) in enumerate(zip(chunks, terminal)):
            enable = pending if i == 0 else (pending & prefix[i])
            self.sync += If(self.clear,
                chunk.eq(0), flag.eq(0)
            ).Elif(enable,
                chunk.eq(chunk + 1), flag.eq(chunk == 0xe)
            )

        # After increment, lower nibble 0 becomes all ones iff its old value
        # was 0xe; every following lower nibble must already have been 0xf.
        for j in range(1, 8):
            lower_was_carrying = chunks[0] == 0xe
            for i in range(1, j):
                lower_was_carrying = lower_was_carrying & terminal[i]
            self.sync += If(self.clear, prefix[j].eq(0)).Elif(
                pending, prefix[j].eq(lower_was_carrying))
