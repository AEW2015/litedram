"""Complete DFI command/write frame pipeline plus a registered receive frame.

Every controller-to-PHY field shares the same edge, keeping commands,
write payload, masks and request enables aligned. The controller's receive
latency must include both command flight and receive capture cycles; write latency stays
unchanged because command and payload each gain the same cycle.
"""

from migen import Module
from migen.genlib.record import DIR_M_TO_S, DIR_S_TO_M


class DFIOutputPipeline(Module):
    def __init__(self, frontend, backend):
        if len(frontend.phases) != len(backend.phases):
            raise ValueError("DFI phase count mismatch")
        for source, destination in zip(frontend.phases, backend.phases):
            if source.layout != destination.layout:
                raise ValueError("DFI phase layout mismatch")
            for name, _, direction in source.layout:
                src, dst = getattr(source, name), getattr(destination, name)
                if direction == DIR_M_TO_S:
                    dst.attr.add("preserve_syn_only")
                    self.sync += dst.eq(src)
                elif direction == DIR_S_TO_M:
                    src.attr.add("preserve_syn_only")
                    self.sync += src.eq(dst)
                else:
                    raise ValueError("Unsupported DFI field direction")
