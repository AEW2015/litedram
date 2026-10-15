"""Native LiteDRAM DMA reader with registered-output response queues."""

from math import log2

from migen import *
from litex.soc.interconnect import stream
from litedram.common import LiteDRAMNativePort
from litedram_phy.fullrate_striped_v17 import OutputRegisteredFIFO


class FullRateDMAReader(Module):
    """Native-only DMA reader preserving LiteDRAM issue/flush semantics.

    Each FIFO has a depth-D synchronous RAM plus the same output pipeline used
    by the striped path. The registered admission flag only admits a push
    while the RAM occupancy predicts room within depth D; the output stages
    provide four additional response entries without putting RAM level on the
    DMA busy/ready path.
    """
    def __init__(self, port, fifo_depth=16, fifo_buffered=True,
                 with_csr=False):
        if not isinstance(port, LiteDRAMNativePort):
            raise NotImplementedError("FullRateDMAReader currently supports native ports")
        if not fifo_buffered:
            raise ValueError("registered output FIFOs require buffered=True")
        self.port = port
        self.enable = enable = Signal(reset=1)
        self.sink = sink = stream.Endpoint([("address", port.address_width)])
        self.source = source = stream.Endpoint([("data", port.data_width)])

        cmd, rdata = port.cmd, port.rdata
        if fifo_depth < 3:
            raise ValueError("fifo_depth must be at least 3")
        # Keep the depth parameter a power of two for the common 64-entry
        # configuration. Migen's buffered SyncFIFO adds its read stage and
        # OutputRegisteredFIFO adds its elastic stages, yielding D+4 entries.
        ram_depth = fifo_depth
        self.submodules.res_fifo = res_fifo = OutputRegisteredFIFO(
            [("dummy", 1)], ram_depth, buffered=True)
        self.submodules.data_fifo = data_fifo = OutputRegisteredFIFO(
            [("data", port.data_width)], ram_depth, buffered=True)

        self.reservation_capacity = fifo_depth + 4
        self.response_capacity = fifo_depth + 4
        self.reservation_level = res_fifo.level
        self.response_level = data_fifo.level

        # Use the registered predictive admission flags. The single-input
        # stream cannot exceed the conservative RAM depth-D bound.
        reserve_admit = res_fifo.admission_allow
        data_admit = data_fifo.admission_allow
        self.comb += [
            cmd.we.eq(0),
            cmd.addr.eq(sink.address),
            cmd.last.eq(sink.last),
            cmd.valid.eq(enable & sink.valid & reserve_admit),
            sink.ready.eq(enable & cmd.ready & reserve_admit),
            res_fifo.sink.valid.eq(cmd.valid & cmd.ready),
            res_fifo.sink.dummy.eq(0),
            res_fifo.sink.last.eq(cmd.last),
            # Responses arrive in command order and cannot overtake the
            # reservation marker. Admission and valid are kept separate so
            # the depth predictor, rather than RAM's level comparator, drives
            # the external ready net.
            data_fifo.sink.valid.eq(rdata.valid & data_admit),
            data_fifo.sink.data.eq(rdata.data),
            data_fifo.sink.last.eq(0),
            rdata.ready.eq(data_admit),
            data_fifo.source.ready.eq(source.ready | ~enable),
            source.data.eq(data_fifo.source.data),
            source.valid.eq(res_fifo.source.valid & data_fifo.source.valid),
            source.last.eq(res_fifo.source.last),
            res_fifo.source.ready.eq(data_fifo.source.valid &
                                     data_fifo.source.ready),
        ]

        # The wrapper is intentionally native-only and does not expose the
        # optional standalone CSR sequencer from LiteDRAMDMAReader.
        if with_csr:
            raise NotImplementedError("DMA CSR sequencing is owned by MemoryDMA")
