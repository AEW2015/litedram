#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Diagnostic-only scheduled native FIFO return capture.

The helper correlates one scheduled FIFO pop with the word present at the
FIFO's front on the actual consume edge.  The selected vendor FIFO model is
front-visible: Q contains the current word before pop, remains that word at
the pop edge, then can become zero/EMPTY by the next half-cycle.  Capturing
on the consume edge therefore holds the pre-pop word for the later DFI
PhaseInjector latch.  This is an explicitly tested model assumption, not a
claim that the native RX bitslice Q is generally valid or that this signal is
functional DFI read-valid.

There is one outstanding request through the PhaseInjector sampling edge.
``timeout`` is an external bounded-diagnostic abort. ``ready`` low, reset,
or timeout cancels a pending request or returned-but-not-yet-retired word.
FIFO_EMPTY is diagnostic only and never controls the scheduled pop.
"""

from migen import If, Module, Signal


class ScheduledNativeReturn(Module):
    """Schedule one pop, capture its front word, and pulse diagnostic valid.

    Request is accepted at edge E0 and the five-bit ``delay`` is sampled
    there.  The registered ``rd_en`` launches at E(delay+1); the synchronous
    FIFO consumes it at E(delay+2).  At that consume edge, all EMPTY bits are
    checked and ``fifo_front`` is captured only if every lane is present.
    ``valid`` is then high for the following cycle, so a PhaseInjector
    sampling on the next edge latches the captured word.  Thus delay=0 and
    delay=31 launch exactly 1 and 32 edges after acceptance, respectively.

    A missing lane suppresses valid and completes the request with an
    ``underflow`` pulse. Requests while outstanding pulse ``overlap`` and
    are rejected, including the cycle in which valid is presented to the
    PhaseInjector. No hardware path should use this diagnostic valid.
    """
    def __init__(self, lanes, data_width):
        if type(lanes) is not int or lanes <= 0:
            raise ValueError("lanes must be a positive integer")
        if type(data_width) is not int or data_width <= 0:
            raise ValueError("data_width must be a positive integer")

        self.read_request = Signal()
        self.delay = Signal(5)
        self.ready = Signal(reset=1)
        self.reset = Signal()
        self.timeout = Signal()  # externally generated diagnostic timeout/abort
        self.fifo_empty = Signal(lanes)
        self.fifo_front = Signal(data_width)

        self.outstanding = Signal()
        self.return_pending = Signal()
        self.request_accepted = Signal()
        self.rd_en = Signal()
        self.captured_word = Signal(data_width)
        self.valid = Signal()  # diagnostic-only pulse; not functional DFI validity
        self.missing_lanes = Signal(lanes)
        self.underflow = Signal()
        self.overlap = Signal()
        self.timed_out = Signal()

        remaining = Signal(5)
        rd_en_reg = Signal()
        returned = Signal()
        valid_reg = Signal()

        # Qualify pulses combinationally as well as clearing registers on the
        # next edge, so abort/reset cannot be sampled as a valid word.
        self.comb += [
            self.return_pending.eq(returned),
            self.rd_en.eq(rd_en_reg & self.ready & ~self.reset & ~self.timeout),
            self.valid.eq(valid_reg & self.ready & ~self.reset & ~self.timeout),
        ]

        self.sync += [
            self.request_accepted.eq(0),
            self.missing_lanes.eq(0),
            self.underflow.eq(0),
            self.overlap.eq(0),
            self.timed_out.eq(0),
            If(self.reset | ~self.ready | self.timeout,
                self.outstanding.eq(0),
                remaining.eq(0),
                rd_en_reg.eq(0),
                returned.eq(0),
                valid_reg.eq(0),
                If(self.timeout, self.timed_out.eq(1))
            ).Else(
                If(self.read_request & self.outstanding,
                    self.overlap.eq(1)
                ),
                If(rd_en_reg,
                    # The native FIFO consumes the previously registered
                    # RD_EN at this edge. Capture front-visible Q now, before
                    # the FIFO can advance Q/EMPTY after this edge.
                    rd_en_reg.eq(0),
                    If(self.fifo_empty != 0,
                        self.missing_lanes.eq(self.fifo_empty),
                        self.underflow.eq(1),
                        self.outstanding.eq(0),
                        returned.eq(0),
                        valid_reg.eq(0)
                    ).Else(
                        self.captured_word.eq(self.fifo_front),
                        valid_reg.eq(1),
                        returned.eq(1)
                    )
                ).Elif(returned,
                    # PhaseInjector samples valid and captured_word on this
                    # edge; retire only afterwards so a same-edge request is
                    # still recognized as an overlap.
                    self.outstanding.eq(0),
                    returned.eq(0),
                    valid_reg.eq(0)
                ).Elif(self.outstanding,
                    If(remaining == 0,
                        rd_en_reg.eq(1)
                    ).Else(
                        remaining.eq(remaining - 1)
                    )
                ).Elif(self.read_request,
                    self.request_accepted.eq(1),
                    self.outstanding.eq(1),
                    remaining.eq(self.delay)
                )
            )
        ]
