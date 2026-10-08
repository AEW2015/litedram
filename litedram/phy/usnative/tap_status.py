#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Registered delay-tap readback with invalidation after selection or tap changes."""

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer
from functools import reduce
from operator import or_


class TapCommandEvents(Module):
    """Fan out synchronized manual tap strobes to requests and invalidation.

    ``commands`` must already be in the PHY system clock domain. Status is
    invalidated for every issued command, including one rejected by
    ``allowed``; only accepted commands become tap requests.
    """
    def __init__(self):
        self.commands  = Signal(4)
        self.allowed   = Signal()
        self.requests  = Signal(4)
        self.invalidate = Signal()

        self.comb += [
            self.requests.eq(self.commands & Replicate(self.allowed, 4)),
            self.invalidate.eq(reduce(or_, [self.commands[i] for i in range(4)])),
        ]


class CSRStatusBridge(Module):
    """Return stable tap status to CSR and register invalidation locally.

    Tap status levels cross from the PHY clock to the CSR clock through
    ``MultiReg``. Invalidation is an event, so it uses a pulse synchronizer.
    Its decoded output is registered in the CSR domain before it gates the
    CSR-facing valid bit; this keeps the synchronizer decode off the CSR read
    mux path. Each observed invalidation holds status low for a bounded quiet
    interval, allowing the source status tree and return synchronizer to settle
    without depending on a separately acknowledged event.
    """
    QUIET_CYCLES = 64

    def __init__(self, width, source_domain, csr_domain):
        if not isinstance(width, int) or width < 1:
            raise ValueError('At least one CSR status bit is required')
        self.source       = Signal(width)
        self.invalidate   = Signal()
        self.source_invalidate = Signal()
        self.status       = Signal(width)

        status_sync = Signal(width)
        self.quiet_count = Signal(max=self.QUIET_CYCLES + 1)
        transfer    = PulseSynchronizer(source_domain, csr_domain)
        self.submodules.transfer = transfer
        self.specials += MultiReg(self.source, status_sync, csr_domain)
        self.comb += transfer.i.eq(self.invalidate)
        getattr(self.sync, csr_domain).__iadd__(If(
            self.source_invalidate | transfer.o,
            self.quiet_count.eq(self.QUIET_CYCLES)
        ).Elif(self.quiet_count != 0,
            self.quiet_count.eq(self.quiet_count - 1)))
        self.comb += self.status.eq(
            status_sync & ~Replicate(self.quiet_count != 0, width))

class RegisteredTapStatus(Module):
    """Registered status tree with explicit settling invalidation.

    change must include every selection and delay-control update. 32 sys cycles
    cover the 2:1 destination clock, pulse CDC and pipeline. Native analog delay
    update settling remains subject to hardware qualification.
    """
    def __init__(self, entries):
        if not isinstance(entries, int) or entries < 1:
            raise ValueError('At least one tap-status entry is required')
        self.source = Signal(9*entries)
        self.select = Signal(max=max(2, entries))
        self.change = Signal()
        self.ready  = Signal()
        self.value  = Signal(9)
        self.valid  = Signal()
        # CSR wrappers use the registered level across the return CDC; the
        # local ``valid`` also includes immediate event invalidation.
        self.csr_valid = Signal()
        local       = Signal(len(self.source))
        sampled     = Signal(len(self.source))
        age         = Signal(6)
        # Capture the wide status bus before reducing it in the sys domain.
        # This uses the documented related clocks, not a general CDC handshake.
        self.sync.riu += local.eq(self.source)
        self.sync += sampled.eq(local)
        # Split selection into registered groups of eight to shorten the mux
        # path. Pad the final group so every low-bit select has a defined value.
        groups = []
        for start in range(0, entries, 8):
            group = Signal(9)
            group.attr.add('dont_touch')
            group_select = Signal(3)
            group_select.attr.add('dont_touch')
            values = [sampled[9*i:9*i+9] for i in range(start, min(start+8, entries))]
            values += [Constant(0, 9)]*(8-len(values))
            # Break the long CSR-to-byte-lane route before the local mux.
            self.sync += [group_select.eq(self.select[:3]),
                group.eq(Array(values)[group_select])]
            groups.append(group)
        # Match the upper selector/range check to the registered group data.
        # This also removes the CSR selector from the final mux's timing path.
        delayed_select = Signal.like(self.select)
        group_index = Signal(max=max(2, len(groups)))
        in_range    = Signal()
        self.sync += [
            delayed_select.eq(self.select),
            group_index.eq(delayed_select[3:] if len(self.select) > 3 else 0),
            in_range.eq(delayed_select < entries),
        ]
        self.sync += [self.value.eq(Mux(in_range, Array(groups)[group_index], 0)),
            If(~self.ready | self.change, age.eq(0)).Elif(age<32, age.eq(age+1))]
        # Only fresh, ready samples may be consumed. The digital wait does
        # not establish the physical delay element's analog settling time.
        # Local users retain immediate invalidation. The CSR wrapper returns
        # the registered level separately and transports ``change`` as an
        # event, so a pulse does not feed the wide CSR read mux.
        self.comb += [
            self.csr_valid.eq((age==32) & self.ready),
            self.valid.eq(self.csr_valid & ~self.change),
        ]
