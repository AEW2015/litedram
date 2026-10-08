#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Optional fabric-side capture for native receive timing measurements."""

from migen import Array, Cat, If, Memory, Module, Signal


def logical_dq_words(logical_rx_data, logical_bits):
    """Select eight-UI words by logical DQ bit number from the adapter bus.

    RXTX tap indices belong to FIFO/status/control vectors. The adapter's
    ``o_dq_rx_data`` bus is already reordered into logical DQ order.
    """
    return Cat(*[logical_rx_data[8*bit:8*(bit+1)] for bit in logical_bits])


def logical_dfi_lane_words(dfi_phases, lane, databits=64):
    """Select one logical x8 lane in outer 8x64 order from physical 4x128 DFI."""
    if type(lane) is not int or not 0 <= lane < databits // 8:
        raise ValueError("DFI trace lane is outside the logical data width")
    if len(dfi_phases) != 4 or any(len(phase.rddata) != 2*databits
            for phase in dfi_phases):
        raise ValueError("Expected physical four-phase double-width DFI read data")
    return Cat(*[
        dfi_phases[ui // 2].rddata[(ui % 2)*databits + lane*8:
            (ui % 2)*databits + (lane + 1)*8]
        for ui in range(8)
    ])


class NativeRXFIFOStatusTrace(Module):
    """Diagnostic-only model of accepted FIFO reads and one-cycle Q latency."""
    def __init__(self, ntaps):
        self.rd_en = Signal(ntaps)
        self.empty = Signal(ntaps)
        self.accepted = Signal(ntaps)
        self.q_valid_model = Signal(ntaps)
        self.comb += self.accepted.eq(self.rd_en & ~self.empty)
        self.sync += self.q_valid_model.eq(self.accepted)


class NativeReadValidSnapshot(Module):
    """Latch DFI data and FIFO state on the first armed read-valid edge.

    The snapshot is single-shot: later reads and FIFO drains cannot replace
    the captured beat until software rearms the module. ``data`` is expected
    to contain all four DFI phases followed by the FIFO empty and read-enable
    vectors, packed in Migen ``Cat`` order.
    """
    def __init__(self, width):
        if width <= 0:
            raise ValueError("Snapshot width must be positive")
        self.arm = Signal()
        self.read_valid = Signal()
        self.data = Signal(width)
        self.snapshot = Signal(width)
        self.done = Signal()

        armed = Signal()
        self.sync += If(self.arm,
            armed.eq(1), self.done.eq(0)
        ).Elif(armed & self.read_valid,
            armed.eq(0), self.done.eq(1), self.snapshot.eq(self.data)
        )


class NativeRXLanePopScoreboard(Module):
    """Check that each DFI read-valid has accepted pops from every DQ FIFO.

    Accepted pops are accumulated per DFI read-valid interval.  The diagnostic
    is intentionally separate from the PHY's functional DFI-valid and FIFO
    drain logic.  ``lane_taps`` contains the eight queried RXTX FIFO tap IDs
    for each x8 DQ lane; strobe and mask FIFOs are not part of the DQ delivery
    check.  Pops are grouped between successive read-valid events, so this is
    intended for diagnostics with at most one unreturned read in flight; it
    does not associate FIFO words with tags for overlapping reads.
    """
    def __init__(self, ntaps, lane_taps):
        if ntaps <= 0 or not lane_taps or any(len(taps) != 8 for taps in lane_taps):
            raise ValueError("Expected eight DQ FIFO taps for every byte lane")
        taps = [tap for lane in lane_taps for tap in lane]
        if any(tap < 0 or tap >= ntaps for tap in taps) or len(set(taps)) != len(taps):
            raise ValueError("DQ FIFO tap IDs must be unique and within the tap range")

        self.accepted = Signal(ntaps)
        self.read_valid = Signal()
        self.clear = Signal()
        self.fault = Signal()
        self.missing_lanes = Signal(len(lane_taps))
        self.completed_reads = Signal(32)
        self._seen = [Signal(8, name="rx_lane{}_seen".format(lane))
                      for lane in range(len(lane_taps))]

        lane_accept = []
        for lane, lane_fifo_taps in enumerate(lane_taps):
            lane_accept.append(Cat(*[self.accepted[tap] for tap in lane_fifo_taps]))

        missing = Cat(*[
            (seen | current) != 0xff
            for seen, current in zip(self._seen, lane_accept)
        ])
        for seen, current in zip(self._seen, lane_accept):
            self.sync += If(self.clear | self.read_valid,
                seen.eq(0)
            ).Else(
                seen.eq(seen | current)
            )
        self.sync += If(self.clear,
            self.fault.eq(0), self.missing_lanes.eq(0), self.completed_reads.eq(0)
        ).Elif(self.read_valid,
            self.missing_lanes.eq(missing),
            self.completed_reads.eq(self.completed_reads + 1),
            If(missing != 0, self.fault.eq(1))
        )


class NativeRXTraceLayout:
    """Bit layout for one packed native RX trace sample.

    Fields follow Migen ``Cat`` order (least significant field first). The
    DQ field is the raw RXTX Q bus: eight Q bits for every DQ pin. FIFO status
    remains tap indexed, so software can map individual taps to lanes using
    the PHY's queried layout. ``byte_phy_rden`` stores the effective PHY_RDEN
    gate per byte lane; ``dfi_read_command`` marks an issued DFI READ.
    """
    def __init__(self, databits, ntaps, dfi_data_width, ncontrols=0,
            ca_width=0, mrs_address_width=0, dqs_counter_lanes=0,
            dm_counter_lanes=0, launched_gate_controls=0, selected_lane=None):
        if (databits <= 0 or ntaps <= 0 or dfi_data_width <= 0 or
                ncontrols < 0 or ca_width < 0 or mrs_address_width < 0 or
                dqs_counter_lanes < 0 or dm_counter_lanes < 0 or
                launched_gate_controls < 0):
            raise ValueError("Trace field widths must be positive")
        if selected_lane is not None and (type(selected_lane) is not int or
                not 0 <= selected_lane < databits // 8):
            raise ValueError("Selected trace lane is outside the logical data width")
        self.selected_lane = selected_lane
        if selected_lane is None:
            dq_word_width = 8*databits
            physical_dfi_width = dfi_data_width
            bitslip_state_width = 3*databits
        else:
            # An x8 lane's Q and returned bit-slip words remain bit-major;
            # the selected physical DFI bytes are packed by outer UI index.
            dq_word_width = 64
            physical_dfi_width = 64
            bitslip_state_width = 24
        fields = (
            ("dq_q", dq_word_width),
            ("dfi_rddata", physical_dfi_width),
            ("fifo_empty", ntaps),
            ("fifo_rd_en", ntaps),
            ("fifo_pop_accepted", ntaps),
            ("fifo_q_valid_model", ntaps),
            ("dq_bitslip_o", dq_word_width),
            ("dq_bitslip_state", bitslip_state_width),
            ("byte_phy_rden", (databits + 7)//8),
            ("dfi_read_command", 1),
            ("rd_input", 1),
            ("read_valid", 1),
            ("ready", 1),
        )
        if ncontrols:
            fields += (("control_phy_rden", 4*ncontrols),)
        if ca_width:
            fields += (("launch_ca", ca_width),)
        if mrs_address_width:
            fields += (("launch_mrs_address", mrs_address_width),)
        if dqs_counter_lanes:
            fields += (("dqs_wrclk_edges", 32*dqs_counter_lanes),)
        if dm_counter_lanes:
            fields += (("dm_wrclk_edges", 32*dm_counter_lanes),)
        if launched_gate_controls:
            fields += (("control_phy_rden_launch", 4*launched_gate_controls),)
        self.fields = {}
        offset = 0
        for name, width in fields:
            self.fields[name] = (offset, width)
            offset += width
        self.width = offset

    def decode(self, sample):
        """Decode an integer trace word to named, unshifted bit fields."""
        if sample < 0 or sample >= (1 << self.width):
            raise ValueError("Trace sample does not fit layout")
        return {name: (sample >> offset) & ((1 << width) - 1)
                for name, (offset, width) in self.fields.items()}

    def pack(self, **values):
        """Pack named fields in the same order used by the HDL ``Cat``."""
        result = 0
        for name, (offset, width) in self.fields.items():
            value = values.get(name, 0)
            if value < 0 or value >= (1 << width):
                raise ValueError("{} does not fit trace field".format(name))
            result |= value << offset
        return result


class NativeRXTrace(Module):
    """Capture the trigger cycle and subsequent samples in the PHY domain.

    Input data and trigger share one register stage so the trace does not add
    a wide RAM-input path to the native FIFO outputs. Rearming cancels an old
    capture. Software must wait for ``done`` before reading the synchronous
    memory port, and allow its address to settle before sampling ``data``.
    """
    def __init__(self, width, depth=64):
        self.arm = Signal()
        self.trigger = Signal()
        self.enable = Signal(reset=1)
        self.sample = Signal(width)
        self.index = Signal(max=depth)
        self.data = Signal(width)
        self.done = Signal()
        self.busy = Signal()

        # # #

        # Preserve the monitor's input boundary through Vivado synthesis. This
        # is instrumentation only; it adds no latency to the functional PHY.
        boundary_attrs = {("keep", "true"), ("dont_touch", "true")}
        staged_sample = Signal(width, attr=boundary_attrs)
        staged_trigger = Signal(attr=boundary_attrs)
        self.sync += [staged_sample.eq(self.sample),
            staged_trigger.eq(self.trigger & self.enable)]
        memory = Memory(width, depth)
        writer = memory.get_port(write_capable=True)
        reader = memory.get_port()
        self.specials += memory, writer, reader
        armed, running = Signal(), Signal()
        pointer = Signal(max=depth)
        start = armed & staged_trigger
        capture = (start | running) & ~self.arm
        self.comb += [
            writer.adr.eq(pointer), writer.dat_w.eq(staged_sample), writer.we.eq(capture),
            reader.adr.eq(self.index), self.data.eq(reader.dat_r),
            self.busy.eq(armed | running),
        ]
        self.sync += If(self.arm,
            armed.eq(1), running.eq(0), pointer.eq(0), self.done.eq(0),
        ).Elif(capture,
            armed.eq(0),
            If(pointer == depth - 1,
                running.eq(0), self.done.eq(1),
            ).Else(running.eq(1), pointer.eq(pointer + 1)),
        )


class NativeRXBoundaryTraceLayout:
    """Compact fields for a repeat-read boundary capture."""
    def __init__(self, raw_dq_width=32, returned_dq_width=32,
            lane_width=8, control_width=8):
        fields = (
            ("read_request", 1),
            ("control_phy_rden", control_width),
            ("lane_fifo_empty", lane_width),
            ("lane_fifo_rd_en", lane_width),
            ("lane_fifo_accepted", lane_width),
            ("dq_raw", raw_dq_width),
            ("dq_returned", returned_dq_width),
            ("read_valid", 1),
            ("repeat_mark", 1),
        )
        self.fields = {}
        offset = 0
        for name, width in fields:
            if width <= 0:
                raise ValueError("Boundary trace field widths must be positive")
            self.fields[name] = (offset, width)
            offset += width
        self.fields["cycle_count"] = (offset, 16)
        self.width = offset + 16
        self.payload_width = offset

    def decode(self, sample):
        if sample < 0 or sample >= (1 << self.width):
            raise ValueError("Boundary trace sample does not fit layout")
        return {name: (sample >> offset) & ((1 << width) - 1)
            for name, (offset, width) in self.fields.items()}


class NativeRXBoundaryTrace(Module):
    """One-shot circular capture with pre-trigger history and a frozen read port.

    The monitor is armed from reset, records continuously into a 32-sample
    ring, then freezes 16 samples before and 16 samples beginning at the first
    synchronized trigger. Its data path is observational only.
    """
    def __init__(self, payload_width, *, depth=32, pretrigger=16):
        if depth != 32 or pretrigger != 16:
            raise ValueError("Boundary trace currently requires a 32-sample, 16-pre ring")
        if payload_width <= 0:
            raise ValueError("Boundary trace payload width must be positive")
        self.mark = Signal()
        self.lane_pop = Signal()
        self.sample = Signal(payload_width)
        self.sample_index = Signal(5)
        self.start_index = Signal(5)
        self.pre_samples = Signal(5)
        self.mark_cycle = Signal(16)
        self.mark_seen = Signal()
        self.trigger_seen = Signal()
        self.trigger_timeout = Signal()
        self.done = Signal()
        self.data = Signal(payload_width + 16)

        memory = Memory(payload_width + 16, depth)
        writer = memory.get_port(write_capable=True)
        reader = memory.get_port(async_read=True)
        self.specials += memory, writer, reader
        pointer = Signal(5)
        cycle_count = Signal(16)
        triggered = Signal()
        mark_pending = Signal()
        mark_wait = Signal(12)
        post_count = Signal(5)
        pre_count = Signal(5)
        trigger_now = ((mark_pending | self.mark) & self.lane_pop & ~triggered)
        self.comb += [
            writer.adr.eq(pointer),
            writer.dat_w.eq(Cat(self.sample, cycle_count)),
            writer.we.eq(~self.done),
            reader.adr.eq((self.start_index + self.sample_index)[:5]),
            self.data.eq(reader.dat_r),
        ]
        self.sync += If(~self.done,
            cycle_count.eq(cycle_count + 1),
            pointer.eq(pointer + 1),
            If(~triggered,
                If(pre_count < pretrigger, pre_count.eq(pre_count + 1)),
                If(self.mark & ~self.mark_seen,
                    self.mark_seen.eq(1), self.mark_cycle.eq(cycle_count),
                    mark_pending.eq(1), mark_wait.eq(0)),
                If(trigger_now,
                    triggered.eq(1), self.trigger_seen.eq(1), post_count.eq(1),
                    self.pre_samples.eq(pre_count),
                    mark_pending.eq(0)
                ).Elif(mark_pending,
                    If(mark_wait == 0xfff,
                        self.trigger_timeout.eq(1), mark_pending.eq(0)
                    ).Else(mark_wait.eq(mark_wait + 1)))
            ).Else(
                If(post_count == 15,
                    self.done.eq(1), self.start_index.eq(pointer + 1)
                ).Else(post_count.eq(post_count + 1))
            )
        )
