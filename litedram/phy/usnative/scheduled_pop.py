#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Diagnostic registered FIFO-pop scheduler for native PHY experiments.

This helper deliberately schedules reads without consulting FIFO_EMPTY.  The
empty vector is sampled only when a scheduled pop fires, for missing-lane
diagnostics.  A pop pulse does not imply that native FIFO Q is valid.
"""

from migen import If, Module, Signal


class ScheduledFIFOPop(Module):
    """Issue one simultaneous, delayed, one-cycle pop across ``lanes``.

    ``read_request`` is accepted only while no request is outstanding.
    ``delay`` is sampled with the request and ranges from 0 through 31.  The
    registered ``rd_en`` launch occurs exactly ``delay + 1`` clock edges
    after the acceptance edge, so zero launches on the next edge.  ``ready``
    going low cancels a pending request.  ``fifo_empty`` is diagnostic only:
    its bits are captured as ``missing_lanes`` on the pop cycle and reduced
    to ``underflow`` at the actual FIFO-consumption edge, one cycle after the
    ``rd_en`` launch.  ``overlap`` pulses when a request arrives while busy.

    The PHY enables this interface only for explicit software diagnostics.
    It does not model or promise the validity timing of RXTX_BITSLICE.Q.
    """
    def __init__(self, lanes):
        if type(lanes) is not int or lanes <= 0:
            raise ValueError("lanes must be a positive integer")

        self.read_request = Signal()
        self.delay = Signal(5)
        self.ready = Signal(reset=1)
        self.reset = Signal()
        self.fifo_empty = Signal(lanes)

        self.busy = Signal()
        self.request_accepted = Signal()
        self.rd_en = Signal()
        self.missing_lanes = Signal(lanes)
        self.underflow = Signal()
        self.overlap = Signal()

        remaining = Signal(5)
        # Output pulses and diagnostics are registered to make them easy to
        # correlate with per-cycle FIFO status traces.
        self.sync += [
            self.request_accepted.eq(0),
            self.rd_en.eq(0),
            self.missing_lanes.eq(0),
            self.underflow.eq(0),
            self.overlap.eq(0),
            # RD_EN is registered.  A native FIFO consumes it at the next
            # edge, so EMPTY must be sampled from that edge, not at launch.
            If(self.rd_en & self.ready & ~self.reset,
                self.missing_lanes.eq(self.fifo_empty),
                self.underflow.eq(self.fifo_empty != 0)
            ),
            If(self.reset | ~self.ready,
                self.busy.eq(0),
                remaining.eq(0)
            ).Else(
                If(self.read_request & self.busy,
                    self.overlap.eq(1)
                ),
                If(self.read_request & ~self.busy,
                    self.request_accepted.eq(1),
                    self.busy.eq(1),
                    remaining.eq(self.delay)
                ).Elif(self.busy,
                    If(remaining == 0,
                        self.rd_en.eq(1),
                        self.busy.eq(0)
                    ).Else(
                        remaining.eq(remaining - 1)
                    )
                )
            )
        ]
