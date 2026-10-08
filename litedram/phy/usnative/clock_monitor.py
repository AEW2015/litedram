#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Opt-in observation of native DQS FIFO write-clock activity."""

from migen import ClockDomain, Module, Signal
from migen.genlib.cdc import MultiReg


def gray_to_binary(gray, width):
    """Convert a Gray-coded counter value to binary."""
    value = gray
    shift = 1
    while shift < width:
        value = value ^ (value >> shift)
        shift <<= 1
    return value


class NativeDQSClockMonitor(Module):
    """Count rising edges of each returned DQS FIFO write clock.

    Each source counter is free-running from configuration and has no reset
    input. Its Gray-coded value crosses into ``sys`` through a two-register
    synchronizer. The source clocks may stop between DDR bursts; software
    should sample after at least four ``sys`` cycles of settling.

    Domain names are stable and byte-indexed because a LiteX SoC has one
    integrated native DDR PHY. Designs with multiple instances must rename
    this module's ``usnative_dqs_wrclkN`` domains at the parent boundary.
    """

    def __init__(self, clocks, *, width=32, connect_clocks=True,
            domain_prefix="usnative_dqs_wrclk", signal_prefix="dqs_wrclk"):
        if not clocks:
            raise ValueError("DQS clock monitor needs at least one lane")
        if not isinstance(width, int) or isinstance(width, bool) or width < 2:
            raise ValueError("DQS clock counter width must be an integer >= 2")
        if not isinstance(connect_clocks, bool):
            raise ValueError("DQS clock connection option must be boolean")
        if not domain_prefix.isidentifier() or not signal_prefix.isidentifier():
            raise ValueError("Clock monitor prefixes must be identifiers")

        self.counts = [Signal(width, name=f"{signal_prefix}_edges{lane}")
                       for lane in range(len(clocks))]
        self.source_clocks = []

        for lane, clock in enumerate(clocks):
            domain = f"{domain_prefix}{lane}"
            clock_domain = ClockDomain(domain, reset_less=True)
            self.clock_domains += clock_domain
            self.source_clocks.append(clock_domain.clk)

            binary = Signal(width, name=f"dqs_wrclk_binary{lane}", reset=0)
            gray = Signal(width, name=f"dqs_wrclk_gray{lane}")
            synced_gray = Signal(width, name=f"dqs_wrclk_gray_sync{lane}")

            next_binary = Signal(width, name=f"dqs_wrclk_next_binary{lane}")
            self.comb += [
                next_binary.eq(binary + 1),
                self.counts[lane].eq(gray_to_binary(synced_gray, width)),
            ]
            if connect_clocks:
                self.comb += clock_domain.clk.eq(clock)
            getattr(self.sync, domain).__iadd__([
                binary.eq(next_binary),
                gray.eq(next_binary ^ (next_binary >> 1)),
            ])
            self.specials += MultiReg(gray, synced_gray, odomain="sys", n=2)
