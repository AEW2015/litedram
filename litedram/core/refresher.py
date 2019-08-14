# This file is Copyright (c) 2016-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# License: BSD

"""LiteDRAM Refresher."""

from migen import *
from migen.genlib.misc import timeline

from litex.soc.interconnect import stream

from litedram.core.multiplexer import *

# RefreshSequencer ---------------------------------------------------------------------------------

class RefreshSequencer(Module):
    """Refresh Sequencer

    Execute the refresh sequence to the DRAM:
    - Send a "Precharge All" command
    - Wait tRP
    - Send an "Auto Refresh" command
    - Wait rRFC
    """
    def __init__(self, cmd, trp, trfc):
        self.start = Signal()
        self.done  = Signal()

        # # #

        self.sync += [
            cmd.a.eq(2**10),
            cmd.ba.eq(0),
            cmd.cas.eq(0),
            cmd.ras.eq(0),
            cmd.we.eq(0),
        ]
        self.sync += [
            self.done.eq(0),
            # Wait start
            timeline(self.start, [
                # Precharge All
                (0,          [cmd.ras.eq(1), cmd.we.eq(1)]),
                # Auto Refresh after tRP
                (trp,        [cmd.cas.eq(1), cmd.ras.eq(1)]),
                # Done after tRP + tRFC
                (trp + trfc, [self.done.eq(1)])
            ])
        ]

# RefreshTimer -------------------------------------------------------------------------------------

class RefreshTimer(Module):
    """Refresh Timer

    Generate periodic pulses (tREFI period) to trigger DRAM refresh.
    """
    def __init__(self, trefi):
        self.wait  = Signal()
        self.done  = Signal()
        self.count = Signal(bits_for(trefi))

        self.load       = Signal()
        self.load_count = Signal(bits_for(trefi))

        # # #

        done  = Signal()
        count = Signal(bits_for(trefi), reset=trefi-1)

        self.sync += [
            If(self.wait,
                If(~self.done,
                    If(self.load & (self.load_count < count),
                        count.eq(self.load_count)
                    ).Else(
                        count.eq(count - 1)
                    )
                )
            ).Else(
                count.eq(count.reset)
            )
        ]
        self.comb += [
            done.eq(count == 0),
            self.done.eq(done),
            self.count.eq(count)
        ]

# Refresher ----------------------------------------------------------------------------------------

class Refresher(Module):
    """Refresher

    Manage DRAM refresh.

    The DRAM needs to be periodically refreshed with a tREFI period to avoid data corruption. During
    a refresh, the controller send a "Precharge All" command to close and precharge all rows and then
    send a "Auto Refresh" command.

    Before executing the refresh, the Refresher advertises the Controller that a refresh should occur,
    this allows the Controller to finis the current transaction and block next transactions. Once all
    transactions are done, the Refresher can execute the refresh Sequence and release the Controller.

    """
    def __init__(self, settings):
        abits  = settings.geom.addressbits
        babits = settings.geom.bankbits + log2_int(settings.phy.nranks)
        self.cmd = cmd = stream.Endpoint(cmd_request_rw_layout(a=abits, ba=babits))

        # # #

        # Refresh Timer ----------------------------------------------------------------------------
        timer = RefreshTimer(settings.timing.tREFI)
        timer = ResetInserter()(timer)
        self.submodules.timer = timer
        self.comb += self.timer.reset.eq(~settings.with_refresh)
        self.comb += self.timer.wait.eq(~self.timer.done)

        # Refresh Sequencer ------------------------------------------------------------------------
        sequencer = RefreshSequencer(cmd, settings.timing.tRP, settings.timing.tRFC)
        self.submodules.sequencer = sequencer

        # Refresh FSM ------------------------------------------------------------------------------
        self.submodules.fsm = fsm = FSM()
        fsm.act("IDLE",
            # Wait periodic Timer pulse
            If(timer.done,
                NextState("WAIT-CONTROLLER-GRANT")
            )
        )
        fsm.act("WAIT-CONTROLLER-GRANT",
            # Advertise Controller, wait grant and start Sequencer
            cmd.valid.eq(1),
            If(cmd.ready,
                sequencer.start.eq(1),
                NextState("WAIT-SEQUENCER")
            )
        )
        fsm.act("WAIT-SEQUENCER",
            # Wait Sequencer and advertise Controller when done
            If(sequencer.done,
                cmd.last.eq(1),
                NextState("IDLE")
            ).Else(
                cmd.valid.eq(1)
            )
        )
