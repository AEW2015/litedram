"""Flag accepted writes that request unsupported byte masks.

The physical interface has no usable data-mask pins, so only a native full
write mask is supported. This guard observes the write-data handshake without
adding logic to the data path.
"""

from migen import Module, Signal


class FullBurstWriteMaskGuard(Module):
    def __init__(self, port):
        self.error = Signal(reset=0)
        all_lanes = (1 << len(port.wdata.we)) - 1
        accepted_partial = port.wdata.valid & port.wdata.ready & (port.wdata.we != all_lanes)
        self.sync += self.error.eq(self.error | accepted_partial)
