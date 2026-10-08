#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Optional native DMA and controller stall counters.

The module adds no logic to a datapath unless explicitly instantiated. Drive
the inputs from the DMA writer/reader, crossbar command port, bank-machine
command valids, and DFI CAS pins to classify bandwidth test stalls.
"""

from migen import *

from litex.soc.interconnect.csr import AutoCSR, CSRStorage, CSRStatus


class LiteDRAMNativeDiagnostics(Module, AutoCSR):
    """Count native request stalls and controller-side command activity.

    Inputs are sampled once per sys clock. ``cas_count`` is the number of
    accepted CAS commands across all DFI phases in that clock (0..nphases).
    ``reader_reservation_full`` should mark cycles where a valid read request
    cannot be admitted because the reader's response reservation FIFO is full.
    """

    def __init__(self):
        self.command_valid            = Signal()
        self.command_ready            = Signal()
        self.bank_command_pending     = Signal()
        self.cas_count                = Signal(4)
        self.writer_fifo_full         = Signal()
        self.reader_reservation_full  = Signal()
        self.clear = Signal()  # Direct reset input for simulation or external control.

        self._clear = CSRStorage()
        self._cycles = CSRStatus(32)
        self._command_stall_cycles = CSRStatus(32)
        self._bank_command_pending_cycles = CSRStatus(32)
        self._controller_cas_commands = CSRStatus(32)
        self._writer_fifo_full_cycles = CSRStatus(32)
        self._reader_reservation_full_cycles = CSRStatus(32)

        cycles = Signal(32)
        command_stall_cycles = Signal(32)
        bank_pending_cycles = Signal(32)
        controller_cas_commands = Signal(32)
        writer_fifo_full_cycles = Signal(32)
        reader_reservation_full_cycles = Signal(32)
        self.comb += [
            self._cycles.status.eq(cycles),
            self._command_stall_cycles.status.eq(command_stall_cycles),
            self._bank_command_pending_cycles.status.eq(bank_pending_cycles),
            self._controller_cas_commands.status.eq(controller_cas_commands),
            self._writer_fifo_full_cycles.status.eq(writer_fifo_full_cycles),
            self._reader_reservation_full_cycles.status.eq(reader_reservation_full_cycles),
        ]
        # The CSR write strobe is a pulse; the stored bit would keep the
        # counters cleared after BIOS writes 1.
        self.sync += If(self.clear | self._clear.re,
            cycles.eq(0),
            command_stall_cycles.eq(0),
            bank_pending_cycles.eq(0),
            controller_cas_commands.eq(0),
            writer_fifo_full_cycles.eq(0),
            reader_reservation_full_cycles.eq(0),
        ).Else(
            cycles.eq(cycles + 1),
            If(self.command_valid & ~self.command_ready,
                command_stall_cycles.eq(command_stall_cycles + 1)
            ),
            If(self.bank_command_pending,
                bank_pending_cycles.eq(bank_pending_cycles + 1)
            ),
            If(self.cas_count != 0,
                controller_cas_commands.eq(controller_cas_commands + self.cas_count)
            ),
            If(self.writer_fifo_full,
                writer_fifo_full_cycles.eq(writer_fifo_full_cycles + 1)
            ),
            If(self.reader_reservation_full,
                reader_reservation_full_cycles.eq(reader_reservation_full_cycles + 1)
            ),
        )
