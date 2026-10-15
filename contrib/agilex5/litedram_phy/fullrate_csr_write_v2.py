"""Pipeline the eight manual write-data CSRs using a registered address decode."""
from migen import *


class ManualWriteDataPipeline(Module):
    def __init__(self, bus, address_select):
        self.pending = pending = Signal()
        self.enable = enable = Signal(8)
        self.data = data = Signal(32, reset_less=True)
        for signal in (pending, enable, data):
            signal.attr.add("no_retiming")
        self.accepted_write = accepted_write = Signal()
        self.accepted_read = accepted_read = Signal()
        selected = accepted_write & (address_select[8:16] != 0)
        self.comb += [
            bus.waitrequest.eq(pending),
            accepted_write.eq(bus.write & ~pending),
            accepted_read.eq(bus.read & ~pending),
        ]
        self.sync += [pending.eq(selected), If(selected, data.eq(bus.writedata))]
        for i in range(8):
            self.sync += enable[i].eq(accepted_write & address_select[8+i])
