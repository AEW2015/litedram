#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Experimental native DDR4 PHY with the initial XEM8320 calibration profile."""

import math
from pathlib import Path
from operator import and_, or_
from functools import reduce

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.soc.interconnect.csr import AutoCSR, CSR, CSRStorage, CSRStatus

from litedram.common import PhySettings, BitSlip, TappedDelayLine, DQSPattern
from litedram.phy.dfi import Interface, DDR4DFIMux
from litedram.phy.usnative.riu_transaction import RIUTransaction
from litedram.phy.usnative.riu_falling_launch import RIUFallingLaunch
from litedram.phy.usnative.tap_status import RegisteredTapStatus, TapCommandEvents

from .core import emit_core
from .adapter import core_ports, signal_sites, connect_core
from .mapping import NativeMapping
from .capabilities import validate_native_configuration
from .pins import device_family, extract_ddr_pins
from .query import query_device


def _native_gate_settings(software_control, trained_gate_delays, selected,
                          lane_delay, global_delay, lane_width):
    """Select per-lane delay, but keep the controller's BL8 gate one cycle.

    The per-lane width is a software calibration control. Once the controller
    owns DFI, only explicitly trained delays persist; width falls back to one.
    """
    manual_override = software_control & selected
    delay_override = selected & (software_control | int(trained_gate_delays))
    return (Mux(delay_override, lane_delay, global_delay),
        Mux(manual_override, lane_width, 1))


def _native_gate_active(gate, remaining, write_level, ready, phy_reset):
    """Drop the active read gate immediately when PHY reset is asserted."""
    return (gate | (remaining != 0) | write_level) & ready & ~phy_reset


_NATIVE_FABRIC_VREF_PROFILE = dict(code=29, mode='FABRIC_RANGE1')


def _native_fabric_vref_profile(enabled):
    """Only opt-in mappings carry the new electrical-profile identity."""
    return ({'fabric_receiver_vref': dict(_NATIVE_FABRIC_VREF_PROFILE)}
        if enabled else {})


def _native_fabric_vref_lanes(layout, physical, databits):
    """Validate one VREF domain per physical x8 lane from resolved LOC data."""
    nbytes = databits // 8
    if len(layout.lanes) != nbytes or tuple(lane.index for lane in layout.lanes) != tuple(range(nbytes)):
        raise ValueError('Fabric receiver VREF needs one ordered physical x8 lane per DQ byte')
    groups = {}
    for lane in layout.lanes:
        keys = ([('dq', bit) for bit in range(8 * lane.index, 8 * lane.index + 8)] +
                [('dm', lane.index), ('dqs_p', lane.index), ('dqs_n', lane.index)])
        try:
            pins = [physical[key] for key in keys]
        except KeyError as error:
            raise ValueError(f'Fabric receiver VREF lane is missing physical pad {error.args[0]}') from error
        group_ids = {(pin.bank, pin.byte) for pin in pins}
        if len(group_ids) != 1:
            raise ValueError(f'Fabric receiver VREF lane {lane.index} crosses physical 13-IO groups')
        positions = [pin.position for pin in pins]
        if len(set(positions)) != len(positions) or any(position not in range(13) for position in positions):
            raise ValueError(f'Fabric receiver VREF lane {lane.index} has invalid 13-IO site positions')
        group = next(iter(group_ids))
        if group in groups:
            raise ValueError('Fabric receiver VREF lanes share a physical 13-IO group')
        groups[group] = lane.index
    return {lane: group for group, lane in groups.items()}


def _add_native_data_iobufs(module, pads, signals, layout, physical, *,
                            databits, dynamic_dci, fabric_receiver_vref):
    """Attach Native DQ/DM buffers, optionally using MIG-style HPIO VREF."""
    lanes = _native_fabric_vref_lanes(layout, physical, databits) if fabric_receiver_vref else {}
    vrefs = {}
    if fabric_receiver_vref:
        for lane, group in sorted(lanes.items()):
            vref = Signal(name=f'native_fabric_vref_lane{lane}')
            module.specials += Instance('HPIO_VREF', attr={('DONT_TOUCH', 'TRUE')},
                p_VREF_CNTR='FABRIC_RANGE1',
                i_FABRIC_VREF_TUNE=Constant(_NATIVE_FABRIC_VREF_PROFILE['code'], 7),
                o_VREF=vref)
            vrefs[lane] = vref
    for name, width, padname in ([('dq', databits, 'dq')] +
            ([('dm_n', databits // 8, 'dm')] if hasattr(pads, 'dm') else [])):
        for bit in range(width):
            byte = bit // 8 if name == 'dq' else bit
            dyn = bit if name == 'dq' else databits + bit
            ports = dict(i_I=signals[f'o_{name}_serial_out'][bit],
                i_T=signals[f'o_{name}_tristate'][bit], i_IBUFDISABLE=0,
                i_DCITERMDISABLE=(signals['o_dyn_dci'][dyn] if dynamic_dci else Constant(0)),
                o_O=signals[f'i_{name}_serial_in'][bit], io_IO=getattr(pads, padname)[bit])
            if fabric_receiver_vref:
                module.specials += Instance('IOBUFE3', p_SIM_DEVICE='ULTRASCALE',
                    p_USE_IBUFDISABLE='FALSE', i_OSC_EN=0, i_OSC=Constant(0, 4),
                    i_VREF=vrefs[byte], **ports)
            else:
                module.specials += Instance('IOBUF_DCIEN', **ports)

# Native receive bitslip ---------------------------------------------------------------------------

class NativeRXBitslip(Module):
    """Register and rotate one gated native FIFO word; never join two bursts.

    This retains the old one-cycle unshifted latency and right-rotation
    convention. Native gated capture supplies complete eight-bit words,
    unlike a continuously sampled component-mode deserializer stream.
    """
    def __init__(self, i, rst, slp, *, held_return=None, accepted=None,
                 captured_word=None, captured_select=None):
        self.o = Signal(8)
        self.shift = Signal(3)
        word = Signal(8)

        # # #

        if held_return is not None:
            if accepted is None:
                raise ValueError("Held RX return requires the corresponding accepted FIFO pop")
            if held_return not in ("pop_edge", "following_edge"):
                raise ValueError("Held RX return must be 'pop_edge' or 'following_edge'")
            # RXTX_BITSLICE FIFO Q timing relative to FIFO_RD_EN is not yet
            # established. Keep both candidate capture edges selectable for
            # hardware A/B; neither mode is presumed correct from the trace.
            if held_return == "pop_edge":
                capture = accepted
            else:
                pending = Signal()
                self.sync += If(rst,
                    pending.eq(0)
                ).Else(
                    pending.eq(accepted)
                )
                capture = pending
            self.sync += If(rst,
                word.eq(0)
            ).Elif(capture,
                word.eq(i)
            )
        else:
            # Preserve the established path exactly: Q is sampled every cycle.
            self.sync += word.eq(i)
        if (captured_word is None) != (captured_select is None):
            raise ValueError("Scheduled RX return requires both captured word and selector")
        rotate_word = word
        if captured_word is not None:
            if len(captured_word) != 8:
                raise ValueError("Scheduled RX capture must be one eight-UI DQ word")
            rotate_word = Signal(8)
            self.comb += rotate_word.eq(Mux(captured_select, captured_word, word))
        self.sync += If(rst,
            self.shift.eq(0)
        ).Elif(slp,
            self.shift.eq(self.shift + 1)
        )
        self.comb += self.o.eq(Array([rotate_word] +
            [Cat(rotate_word[n:], rotate_word[:n]) for n in range(1, 8)])[self.shift])


def _native_data_phase(latency):
    """Return the DFI data phase after the native PHY's one-CK command delay."""
    return (-(latency + 1)) % 4


def _native_effective_read_latency(base, held_rx_return, override=None):
    """Resolve the final PHY read latency, including an optional diagnostic override."""
    if held_rx_return == 'following_edge':
        base += 1
    if override is None:
        return base
    if (isinstance(override, bool) or not isinstance(override, int) or
            override < base or override > 18):
        # The read pipeline has 32 taps and its status CSR has five bits.
        # Two additional cycles let fixed-pop trials absorb routed lane skew
        # at 3200 without altering CAS timing or the burst issue interval.
        raise ValueError('Read latency override must be an integer from the effective profile value through 18')
    return override


def _native_latency_profile(frequency, latency_profile):
    """Return an explicitly selected opt-in latency tuple, or None for defaults."""
    if latency_profile is None:
        return None
    if type(latency_profile) is not str or latency_profile != '2400_cl18_cwl16':
        raise ValueError("Unsupported Native latency profile; expected '2400_cl18_cwl16'")
    if int(round(frequency)) != 300000000:
        raise ValueError("Native latency profile '2400_cl18_cwl16' requires a 300 MHz PHY clock")
    return (18, 16, _native_data_phase(18), _native_data_phase(16), 13, 4, 3)


def _native_fixed_fifo_pop_request(rd_taps, read_latency, ready, reset):
    """Request a global FIFO pop one tap before the fixed DFI-valid edge."""
    if (not isinstance(read_latency, int) or isinstance(read_latency, bool) or
            read_latency < 2 or read_latency > len(rd_taps)):
        raise ValueError('Fixed FIFO pop requires read latency 2..number of read taps')
    return rd_taps[read_latency - 2] & ready & ~reset


class NativeFixedReadTappedDelayLine(Module):
    """Fixed-valid read pipeline that drops pending reads while not ready."""
    def __init__(self, signal, ntaps, flush):
        self.input = signal
        self.taps = Array(Signal.like(signal) for _ in range(ntaps))
        self.output = self.taps[-1]
        self.sync += If(flush,
            *[tap.eq(0) for tap in self.taps]
        ).Else(
            *[tap.eq(self.input if index == 0 else self.taps[index - 1])
              for index, tap in enumerate(self.taps)]
        )


class NativeFixedFIFOPopMonitor(Module):
    """Count fixed scheduled pops that encounter missing x8-lane FIFO words."""
    def __init__(self, lanes, *, pipeline=False):
        if not 1 <= lanes <= 8:
            raise ValueError('Fixed FIFO pop monitor supports one to eight lanes')
        if not isinstance(pipeline, bool):
            raise ValueError('Fixed FIFO pop monitor pipeline option must be boolean')
        self.pop = Signal()
        self.available = Signal(lanes)
        self.clear = Signal()
        self.reset = Signal()
        self.underflows = Signal(32)
        self.missing = Signal(lanes)
        missing_now = ~self.available
        any_missing = ~reduce(and_, [self.available[i] for i in range(lanes)])
        if pipeline:
            # Register the wide FIFO availability reduction away from the
            # saturating counter enable. The CSR result is delayed one cycle;
            # a pending pop is consumed while the next pop is captured, so
            # continuous strobes remain individually accounted.
            pending_pop = Signal()
            pending_missing = Signal(lanes)
            pending_any_missing = reduce(or_,
                [pending_missing[i] for i in range(lanes)])
            self.sync += If(self.clear | self.reset,
                self.underflows.eq(0), self.missing.eq(0),
                pending_pop.eq(0), pending_missing.eq(0)
            ).Else(
                If(pending_pop,
                    self.missing.eq(pending_missing),
                    If(pending_any_missing,
                        If(self.underflows != 0xffffffff,
                            self.underflows.eq(self.underflows + 1)
                        )
                    )
                ),
                pending_pop.eq(self.pop),
                pending_missing.eq(missing_now)
            )
        else:
            self.sync += If(self.clear | self.reset,
                self.underflows.eq(0), self.missing.eq(0)
            ).Elif(self.pop,
                self.missing.eq(missing_now),
                If(any_missing,
                    If(self.underflows != 0xffffffff,
                        self.underflows.eq(self.underflows + 1)
                    )
                )
            )


def _native_write_snapshot(phases):
    """Pack phase CA and write data/masks/enables into 32-bit CSR words."""
    command_words = [Cat(p.address, p.bank, p.act_n, p.ras_n, p.cas_n,
                         p.we_n, p.cs_n, p.cke, p.odt) for p in phases]
    data = Cat(*[Cat(p.wrdata, p.wrdata_mask, p.wrdata_en) for p in phases])
    return command_words, data


class NativeWriteSnapshot(Module):
    """Capture recent CA history and complete DFI state on write enable.

    The first four words hold four contiguous command-history records. The
    following words hold all phase CA/control vectors and phase write data,
    masks and enables. Raw phases and trigger cross a preserved input-register
    boundary together; valid/data become visible one cycle after trigger.
    Word offsets derive from the DFI widths.
    """
    def __init__(self, phases):
        self.trigger = Signal()
        self.clear = Signal()
        self.valid = Signal()
        self.count = Signal(32)
        self.command = Signal(8)
        self.address = Signal(17)

        # Snapshot the full raw DFI phase vector before command selection,
        # decode, history, or packed-data fanout. These keep/dont_touch attrs
        # are diagnostic timing boundaries, not PHY data-path constraints.
        phase_fields = ("address", "bank", "act_n", "ras_n", "cas_n",
                        "we_n", "cs_n", "cke", "odt", "wrdata",
                        "wrdata_mask", "wrdata_en")
        boundary_attrs = {("keep", "true"), ("dont_touch", "true")}
        staged_phases = []
        for phase_index, phase in enumerate(phases):
            staged = type("NativeWriteSnapshotPhase", (), {})()
            for field in phase_fields:
                source = getattr(phase, field)
                staged_signal = Signal(len(source),
                    name=f"write_snapshot_stage_p{phase_index}_{field}",
                    attr=boundary_attrs)
                setattr(staged, field, staged_signal)
                self.sync += staged_signal.eq(source)
            staged_phases.append(staged)
        staged_trigger = Signal(name="write_snapshot_stage_trigger",
            attr=boundary_attrs)
        self._staged_trigger = staged_trigger
        self.sync += If(self.clear, staged_trigger.eq(0)).Else(
            staged_trigger.eq(self.trigger))

        command_words, data = _native_write_snapshot(staged_phases)
        active = [~phase.cs_n for phase in staged_phases]
        selected = Signal(2)
        selected_valid = Signal()
        self.comb += [selected.eq(0), selected_valid.eq(0)]
        for index in reversed(range(len(phases))):
            self.comb += If(active[index], selected.eq(index), selected_valid.eq(1))
        selected_address = Array([phase.address for phase in staged_phases])[selected]
        selected_command = Cat(
            Array([phase.cs_n for phase in staged_phases])[selected],
            Array([phase.cas_n for phase in staged_phases])[selected],
            Array([phase.ras_n for phase in staged_phases])[selected],
            Array([phase.we_n for phase in staged_phases])[selected],
            Array([phase.act_n for phase in staged_phases])[selected])
        # A compact valid/index/address/bank/command record for each prior cycle.
        selected_bank = Array([phase.bank for phase in staged_phases])[selected]
        current_record = Cat(selected_valid, selected, selected_address,
                            selected_bank, selected_command)
        history_width = len(current_record) * 4
        history_words = (history_width + 31) // 32
        command_vector = Cat(*command_words)
        command_width = len(command_vector)
        command_words_count = (command_width + 31) // 32
        data_width = len(data)
        data_words_count = (data_width + 31) // 32
        self.history_word_count = history_words
        self.command_word_offset = history_words
        self.data_word_offset = history_words + command_words_count
        word_count = self.data_word_offset + data_words_count
        self.words = [Signal(32, name=f'write_snapshot_word{i}') for i in range(word_count)]

        history = Signal(history_width)
        self.sync += history.eq(Cat(current_record, history[:-len(current_record)]))
        packed = Cat(history, Constant(0, history_words*32-history_width),
                     command_vector, Constant(0, command_words_count*32-command_width),
                     data, Constant(0, data_words_count*32-data_width))
        self.sync += If(self.clear,
            self.valid.eq(0), self.count.eq(0), self.command.eq(0), self.address.eq(0),
            *[word.eq(0) for word in self.words]
        ).Elif(staged_trigger,
            self.valid.eq(1), self.count.eq(self.count + 1),
            self.command.eq(Cat(selected_command, selected, selected_valid)),
            self.address.eq(Mux(selected_valid, selected_address, 0)),
            *[word.eq(packed[32*i:32*(i+1)]) for i, word in enumerate(self.words)]
        )


class NativeDebugTraceCapture(Module):
    """Stage debug-ring inputs and preserve the first post-trigger sample.

    Raw data and its trigger cross the same kept register boundary. The
    one-cycle trigger delay is matched to the data delay, so the first RAM
    write still samples the first source cycle after the raw trigger. This is
    an optional debug trace path and does not alter the PHY return datapath.
    """
    def __init__(self, sample_width, depth=64):
        if depth != 64:
            raise ValueError("Native debug trace depth is fixed at 64 samples")
        self.arm = Signal()
        self.trigger = Signal()
        self.sample = Signal(sample_width)
        self.write_enable = Signal()
        self.write_address = Signal(6)
        self.write_data = Signal(sample_width)
        self.pending = Signal()
        self.running = Signal()
        self.done = Signal()

        # Keep both paths at the capture boundary. A register on data alone
        # would move the ring by one sample relative to an unregistered event.
        attrs = {("keep", "true"), ("dont_touch", "true")}
        staged_sample = Signal(sample_width, name="debug_trace_stage_sample",
                               attr=attrs)
        staged_trigger = Signal(name="debug_trace_stage_trigger", attr=attrs)
        self._staged_sample = staged_sample
        self._staged_trigger = staged_trigger
        pointer = Signal(6)

        self.sync += staged_sample.eq(self.sample)
        self.sync += If(self.arm, staged_trigger.eq(0)).Else(
            staged_trigger.eq(self.trigger))
        self.comb += [self.write_enable.eq(self.running),
            self.write_address.eq(pointer), self.write_data.eq(staged_sample)]
        self.sync += If(self.arm,
            self.pending.eq(1), self.running.eq(0), pointer.eq(0), self.done.eq(0)
        ).Elif(self.pending & staged_trigger,
            self.pending.eq(0), self.running.eq(1), pointer.eq(0)
        ).Elif(self.running,
            If(pointer == 63,
                self.running.eq(0), self.done.eq(1)
            ).Else(pointer.eq(pointer + 1)))


def _lane_aligned_fifo_available(module, lane_available):
    """Build byte-local copies of the all-lanes-ready condition.

    Every byte lane must advance together, but driving every native FIFO read
    enable from one common net creates a large placement-sensitive fanout.
    Factor the final lane term locally so synthesis can keep each byte's read
    enables near its own availability logic.  This is combinational and keeps
    the original cycle behavior; it does not add latency to FIFO_EMPTY.
    """
    result = []
    for lane, available in enumerate(lane_available):
        other_lanes = [value for index, value in enumerate(lane_available) if index != lane]
        other_available = reduce(and_, other_lanes) if other_lanes else Constant(1)
        local_available = Signal(name=f"fifo_lane{lane}_aligned_available")
        result.append(local_available)
        # Leave synthesis and physical optimization free to replicate or
        # refactor this term near its native FIFO sinks.
        module.comb += local_available.eq(available & other_available)
    return result


def _registered_fifo_lane_drains(module, lane_available, ready, reset):
    """Intermediate timing/safety experiment for registered native FIFO reads.

    The lane availability bits are sampled together in the FIFO read-clock
    domain. A read consumes that sample, then one cycle is left idle before a
    new sample is accepted. This avoids issuing another read from the same
    potentially stale FIFO_EMPTY observation. ``ready`` gates both sampling
    and reads, and ``reset`` cancels any scheduled read. This does not reserve
    capacity for reads in flight or schedule returned tokens. The generated
    core currently does not expose a verified FIFO Q response latency or the
    exact FIFO_EMPTY pointer-visibility delay, and this PHY has no measured
    mapping from ``rd.input`` to the number of native FIFO words per BL8.
    Consequently this mode cannot safely reserve tokens for consecutive
    reads. The next step is to obtain those contracts from the native
    primitive simulation, then add per-lane outstanding-read accounting and
    ordered response assembly before enabling sustained back-to-back traffic.
    """
    sampled = Signal(len(lane_available), name="fifo_lane_available_sampled")
    all_sampled = reduce(and_, (sampled[lane] for lane in range(len(lane_available))))
    drains = []
    for lane, available in enumerate(lane_available):
        drain = Signal(name=f"fifo_lane{lane}_registered_drain")
        drains.append(drain)
        module.comb += drain.eq(ready & ~reset & all_sampled)
        module.sync += If(reset,
            sampled[lane].eq(0)
        ).Elif(ready & ~all_sampled,
            sampled[lane].eq(available)
        ).Else(
            sampled[lane].eq(0)
        )
    return drains


def _read_token_fifo_lane_drains(module, lane_available, read_issue,
                                 read_valid, ready, reset, *,
                                 idle_status=None, epoch_flush=None):
    """Flush idle FIFO data and expire each unmatched pop allowance at valid.

    The native FIFO can contain old words when a new READ is issued. Drain
    them while no command is in flight, then hold the selected word until
    fixed-latency DFI read-valid. A short per-lane request history marks which
    READs remain unmatched, so a missing word's allowance expires on that
    READ's valid edge instead of leaking into a later command. Each accepted
    pop is assigned to the oldest unmatched READ. This remains diagnostic-only:
    with overlapping READs and lane-skewed arrivals, it cannot prove command
    association across lanes; use at most sixteen outstanding READs.

    Expiry is not a quarantine for late DQS writes: delayed EMPTY visibility
    can hide an old word on the next issue edge. Separated diagnostic probes
    must allow a quiet interval and verify empty before issuing another READ.
    Back-to-back command association is not guaranteed by this helper.

    Idle flushing is deliberately sparse: after any pop, wait two complete
    read-clock edges before another unqualified idle pop. This is specific to
    this diagnostic burst-FIFO path, where EMPTY may take two read clocks to
    reflect the final pointer update. It is not the production FIFO drain.
    """
    request_depth = 16
    in_flight = Signal(5, name="fifo_reads_in_flight")
    idle = Signal(name="fifo_idle_flush")
    retire_valid = Signal()
    issue_token = Signal()
    module.comb += [
        retire_valid.eq(read_valid & (in_flight != 0)),
        issue_token.eq(read_issue & ((in_flight < request_depth) | retire_valid)),
    ]
    pending_states = []
    epoch_flush = Constant(0) if epoch_flush is None else epoch_flush
    drains = []
    selected_pops = []
    for lane, available in enumerate(lane_available):
        pending = Signal(5, name=f"fifo_lane{lane}_pending_tokens")
        pending_states.append(pending)
        unmatched = Signal(request_depth, name=f"fifo_lane{lane}_read_tokens")
        saw_empty = Signal(name=f"fifo_lane{lane}_post_issue_empty")
        idle_flush_wait = Signal(2, name=f"fifo_lane{lane}_idle_flush_wait")
        token_pop = Signal(name=f"fifo_lane{lane}_token_pop")
        idle_flush_pop = Signal(name=f"fifo_lane{lane}_idle_flush_pop")
        drain = Signal(name=f"fifo_lane{lane}_token_drain")
        popped_requests = Signal(request_depth,
            name=f"fifo_lane{lane}_requests_after_pop")
        requests_after_valid = Signal(request_depth,
            name=f"fifo_lane{lane}_requests_after_valid")
        requests_next = Signal(request_depth,
            name=f"fifo_lane{lane}_requests_next")
        oldest_unmatched = Array([popped_requests[index]
            for index in range(request_depth)])[Mux(in_flight != 0, in_flight - 1, 0)]
        expire_token = Signal(name=f"fifo_lane{lane}_expire_token")
        drains.append(drain)
        selected_pops.append(token_pop)
        # A READ must never consume a word that was already available on its
        # issue edge. If it was nonempty at issue, require an empty observation
        # before accepting a later word; stale data must fail closed.
        module.comb += [
            token_pop.eq(~reset & ready & available & saw_empty & (pending != 0)),
            # EMPTY can take two read-clock edges to reflect the read pointer
            # after the final FIFO word is popped. Space speculative idle
            # flush pops by those two edges; qualified READ-token pops remain
            # ungated so back-to-back returned words are not delayed.
            idle_flush_pop.eq(idle & available & (idle_flush_wait == 0) & ~epoch_flush),
            drain.eq(token_pop | idle_flush_pop),
            expire_token.eq(retire_valid & oldest_unmatched),
        ]
        for request in range(request_depth):
            older_unmatched = (reduce(or_, (unmatched[index]
                for index in range(request + 1, request_depth)))
                if request + 1 < request_depth else Constant(0, 1))
            module.comb += [
                # Bit zero is newest; the highest set request bit is the
                # oldest unmatched READ and receives this lane's next word.
                popped_requests[request].eq(unmatched[request] &
                    ~(token_pop & ~older_unmatched)),
                requests_after_valid[request].eq(popped_requests[request] &
                    ~(retire_valid & (in_flight == request + 1))),
            ]
        # Shift the request history toward older slots on each issue. A valid
        # edge first retires the oldest slot; simultaneous issue/valid therefore
        # replaces the retired entry without losing the new READ's allowance.
        module.comb += requests_next[0].eq(issue_token | requests_after_valid[0])
        for request in range(1, request_depth):
            module.comb += requests_next[request].eq(
                Mux(issue_token, requests_after_valid[request - 1],
                    requests_after_valid[request]))
        module.sync += If(reset | ~ready,
            saw_empty.eq(0)
        ).Else(
            If(issue_token & (pending == 0),
                saw_empty.eq(~available)
            ).Elif((pending != 0) & ~available,
                saw_empty.eq(1)
            )
        )
        module.sync += If(reset | ~ready,
            idle_flush_wait.eq(0)
        ).Elif(drain | epoch_flush,
            # Count every physical pop, including a qualified token pop, so
            # a later idle flush cannot outrun delayed EMPTY visibility.
            idle_flush_wait.eq(2)
        ).Elif(read_issue,
            # A new command starts its own EMPTY-qualification epoch. This
            # state does not gate token_pop itself.
            idle_flush_wait.eq(0)
        ).Elif(idle_flush_wait != 0,
            idle_flush_wait.eq(idle_flush_wait - 1)
        )
        module.sync += If(reset | ~ready,
            pending.eq(0), unmatched.eq(0)
        ).Else(
            unmatched.eq(requests_next),
            # A pop clears the oldest unmatched entry before the valid edge
            # checks it, so pop and expiry cannot both retire one lane token.
            # Keep the update explicit to avoid narrow-signal add/subtract wrap.
            If(issue_token & (token_pop | expire_token),
                pending.eq(pending)
            ).Elif(token_pop | expire_token,
                pending.eq(pending - 1)
            ).Elif(issue_token,
                pending.eq(pending + 1)
            )
        )
    module.sync += If(reset | ~ready,
        in_flight.eq(0)
    ).Else(
        If(issue_token & ~retire_valid, in_flight.eq(in_flight + 1)
        ).Elif(retire_valid & ~issue_token, in_flight.eq(in_flight - 1))
    )
    idle_terms = [~reset, ready, ~read_issue, ~read_valid, (in_flight == 0)]
    idle_terms.extend(pending == 0 for pending in pending_states)
    module.comb += idle.eq(reduce(and_, idle_terms))
    if idle_status is not None:
        module.comb += idle_status.eq(idle)
    return drains, selected_pops


def _native_fifo_epoch_flush(module, request, owner, ready, reset, scheduled_mode,
                             token_idle, fifo_empty, flushable_taps, accepted_count):
    """Issue one guarded read-clock pulse to each mapped, nonempty RX FIFO.

    This software-owned epoch cleanup is deliberately separate from normal
    READ-token accounting. Only DQ, DQS and DM taps supplied by the validated
    native lane map are eligible; clock/control and unmapped taps remain low.
    A rejected CSR strobe is dropped, never queued for a later unsafe epoch.
    """
    ntaps = len(fifo_empty)
    flushable_taps = tuple(flushable_taps)
    if len(set(flushable_taps)) != len(flushable_taps):
        raise ValueError("Epoch-flush taps must be unique")
    if any(type(tap) is not int or not 0 <= tap < ntaps for tap in flushable_taps):
        raise ValueError("Epoch-flush taps must be mapped native FIFO indices")

    cooldown = Signal(2, name="fifo_epoch_flush_cooldown")
    accepted = Signal(name="fifo_epoch_flush_accepted")
    tap_read_enable = Signal(ntaps, name="fifo_epoch_flush_rd_en")
    allowed = owner & ready & ~reset & ~scheduled_mode & token_idle & (cooldown == 0)
    module.comb += accepted.eq(request & allowed)
    flushable = set(flushable_taps)
    for tap in range(ntaps):
        pulse = accepted & ~fifo_empty[tap] if tap in flushable else Constant(0)
        module.comb += tap_read_enable[tap].eq(pulse)
    module.sync += [
        If(reset | ~ready,
            cooldown.eq(0)
        ).Elif(accepted,
            cooldown.eq(2)
        ).Elif(cooldown != 0,
            cooldown.eq(cooldown - 1)
        ),
        If(accepted, accepted_count.eq(accepted_count + 1)),
    ]
    return accepted, tap_read_enable


# Ultrascale native DDR4 PHY -----------------------------------------------------------------------

class USNativeDDRPHY(Module, AutoCSR):
    def __init__(self, pads, platform, pll_clk, pll_locked, pll_enable,
                 *, sys_clk_freq, output_dir, vivado='vivado', with_debug=False, overclock=False,
                 csr_cdc=lambda signal: signal,
                 csr_status_cdc=None,
                 riu_domain='riu', registered_tx=True,
                 query_cache_dir=None, query_force_refresh=False, queried_topology=None,
                 local_fifo_drain=False, registered_common_fifo_drain=False,
                 registered_fifo_drain=False, read_token_fifo_drain=False,
                 fixed_fifo_pop=False,
                 fixed_fifo_pop_monitor_pipeline=False,
                 held_rx_return=None,
                 read_latency_override=None,
                 latency_profile=None,
                 refclk_attribute_mhz=None,
                 data_tbyte=False,
                 pre_emphasis=False, dynamic_odelay=False, dynamic_dci=True,
                 with_write_monitor=False, with_read_monitor=False, pipeline_write_data=False,
                 write_data_advance=0, write_data_pipeline_cycles=None,
                 with_dqs_wrclk_monitor=False, with_dm_wrclk_monitor=False,
                 with_mrs_command_trace=False,
                 with_scheduled_fifo_pop=False,
                 with_scheduled_fifo_return=False,
                 with_rx_trace=None,
                 rx_trace_lane=None,
                 with_rx_boundary_monitor=False,
                 trained_gate_delays=False,
                 fabric_receiver_vref=False, is_rdimm=False, rcd_latency=1,
                 memtype='DDR4', core_module_name='usnative_core'):
        from litex.build.xilinx.vivado import XilinxVivadoToolchain

        # Profile and native resources -------------------------------------------------------------

        if not isinstance(platform.toolchain, XilinxVivadoToolchain):
            raise ValueError('USNativeDDRPHY requires the Vivado toolchain')
        if not isinstance(fabric_receiver_vref, bool):
            raise ValueError('Fabric receiver VREF option must be boolean')
        if fabric_receiver_vref and memtype != 'DDR4':
            raise ValueError('Fabric receiver VREF is supported only for native DDR4')
        if fabric_receiver_vref and device_family(platform.device) != 'ULTRASCALE_PLUS':
            raise ValueError('Fabric receiver VREF requires an UltraScale+ device')
        latency_profile_values = _native_latency_profile(sys_clk_freq, latency_profile)
        if data_tbyte:
            raise ValueError('DQ/DM TBYTE mode is unsafe during write leveling: '
                'the shared nibble output enable cannot release DQ while driving DQS')
        if not isinstance(fixed_fifo_pop, bool):
            raise ValueError('Fixed FIFO pop option must be boolean')
        if not isinstance(fixed_fifo_pop_monitor_pipeline, bool):
            raise ValueError('Fixed FIFO pop monitor pipeline option must be boolean')
        if fixed_fifo_pop_monitor_pipeline and not fixed_fifo_pop:
            raise ValueError('Fixed FIFO pop monitor pipeline requires fixed_fifo_pop')
        if fixed_fifo_pop and any((local_fifo_drain, registered_common_fifo_drain,
                registered_fifo_drain, read_token_fifo_drain,
                with_scheduled_fifo_pop, with_scheduled_fifo_return)):
            raise ValueError('Fixed FIFO pop is mutually exclusive with registered/token/scheduled FIFO modes')
        if registered_fifo_drain:
            raise ValueError('Registered FIFO drain cannot sustain consecutive reads: '
                'its idle cycle leaves DFI read-valid without a new FIFO word')
        if read_token_fifo_drain and memtype != 'DDR4':
            raise ValueError('Read-token FIFO drain is available only for native DDR4 diagnostics')
        if dynamic_odelay:
            raise ValueError('Dynamic output delay is disabled until its tap readback, '
                'VTC ownership and per-DQ scan restoration are validated in hardware')
        if not isinstance(dynamic_dci, bool):
            raise ValueError('Dynamic DCI option must be boolean')
        if not isinstance(trained_gate_delays, bool):
            raise ValueError('Trained gate-delay option must be boolean')
        if not isinstance(with_dqs_wrclk_monitor, bool):
            raise ValueError('DQS FIFO write-clock monitor option must be boolean')
        if not isinstance(with_dm_wrclk_monitor, bool):
            raise ValueError('DM FIFO write-clock monitor option must be boolean')
        if with_dm_wrclk_monitor and (not with_dqs_wrclk_monitor or not with_read_monitor):
            raise ValueError('DM clock monitor requires DQS clock and read monitors')
        if with_rx_trace is None:
            with_rx_trace = with_read_monitor
        if not isinstance(with_rx_trace, bool):
            raise ValueError('RX trace option must be boolean')
        if with_rx_trace and not with_read_monitor:
            raise ValueError('RX trace requires the read monitor')
        if rx_trace_lane is not None:
            if (isinstance(rx_trace_lane, bool) or not isinstance(rx_trace_lane, int) or
                    not with_rx_trace):
                raise ValueError('Selected RX trace lane requires an enabled RX trace and integer lane')
        if not isinstance(with_rx_boundary_monitor, bool):
            raise ValueError('RX boundary monitor option must be boolean')
        if with_rx_boundary_monitor and not with_read_monitor:
            raise ValueError('RX boundary monitor requires the read monitor')
        if not isinstance(with_mrs_command_trace, bool):
            raise ValueError('MRS command trace option must be boolean')
        if with_mrs_command_trace and not with_rx_trace:
            raise ValueError('MRS command trace requires the RX trace')
        if with_scheduled_fifo_pop and not (with_read_monitor and read_token_fifo_drain):
            raise ValueError("Scheduled FIFO pop requires read-token and read monitors")
        if with_scheduled_fifo_return and not with_scheduled_fifo_pop:
            raise ValueError("Scheduled FIFO return requires scheduled FIFO pop mode")
        dm_monitor_lanes = (0, 1) if with_dm_wrclk_monitor else ()
        if csr_status_cdc is None:
            def csr_status_cdc(signal, invalidation=None, source_invalidation=None):
                status = Signal(len(signal))
                invalidate = [event for event in (invalidation, source_invalidation)
                    if event is not None]
                self.comb += status.eq(signal & ~reduce(or_, invalidate)) if invalidate else status.eq(signal)
                return status
        if sum(bool(mode) for mode in (local_fifo_drain, registered_common_fifo_drain,
                                       registered_fifo_drain, read_token_fifo_drain)) > 1:
            raise ValueError('Select only one native FIFO drain mode')
        if held_rx_return not in (None, False, 'pop_edge', 'following_edge'):
            raise ValueError("Held RX return must be None, 'pop_edge' or 'following_edge'")
        if held_rx_return is False:
            held_rx_return = None
        if fixed_fifo_pop and held_rx_return != 'pop_edge':
            raise ValueError("Fixed FIFO pop requires held_rx_return='pop_edge'")
        if refclk_attribute_mhz is not None:
            if (isinstance(refclk_attribute_mhz, bool) or
                    not isinstance(refclk_attribute_mhz, (int, float)) or
                    not math.isfinite(refclk_attribute_mhz) or refclk_attribute_mhz <= 0):
                raise ValueError('RXTX reference-frequency attribute must be finite and positive')
        if write_data_advance not in (0, 1) or (write_data_advance and not pipeline_write_data):
            raise ValueError('Write-data advance requires the native data pipeline')
        if write_data_pipeline_cycles is not None:
            if (not isinstance(write_data_pipeline_cycles, int) or
                    not 0 <= write_data_pipeline_cycles <= 6 or write_data_advance):
                raise ValueError('Explicit write pipeline must be 0..6 cycles without write-data advance')
        widths = dict(a=14, we_n=1, cas_n=1, ras_n=1, ba=2, act_n=1,
            clk_p=1, clk_n=1, cs_n=1,
            cke=1, odt=1, reset_n=1)
        if any(not hasattr(pads, name) or len(getattr(pads, name)) != width
               for name, width in widths.items()):
            raise ValueError('Integrated native PHY requires single-rank DDR4 with '
                'a14 plus we/cas/ras, ba2 and one CK pair')
        if not hasattr(pads, 'bg') or len(pads.bg) not in (1, 2):
            raise ValueError('Integrated native DDR4 PHY requires one or two BG pins')
        databits = len(pads.dq)
        if databits not in (16, 32, 64):
            raise ValueError('Integrated native DDR4 PHY requires x16, x32 or x64 DQ pads')
        nlanes = len(pads.dqs_p)
        group_width = databits // nlanes
        with_dm = hasattr(pads, 'dm')
        if group_width not in (4, 8) or databits != group_width*nlanes or len(pads.dqs_n) != nlanes:
            raise ValueError('Native PHY needs x4 or x8 strobe groups')
        if with_dm and (group_width != 8 or len(pads.dm) != databits//8):
            raise ValueError('DM is supported only on x8 groups')
        if group_width == 4 and (fabric_receiver_vref or with_dm_wrclk_monitor or rx_trace_lane is not None):
            raise ValueError('x8-only electrical/trace diagnostics are unavailable on x4')
        if rx_trace_lane is not None and not 0 <= rx_trace_lane < nlanes:
            raise ValueError('Selected RX trace lane is outside the physical DQ width')
        if is_rdimm and rcd_latency != 1:
            raise ValueError('This RDIMM candidate supports one registered command clock')
        if not registered_tx or riu_domain != 'riu':
            raise ValueError('Native profile requires registered TX and related sys:riu 2:1 clocks')
        frequency = int(round(sys_clk_freq))
        profiles = {
            # Native full-rate x64 hardware returns the final lane by
            # cycle 11. Cycle 12 lets a following tCCD=8 burst overwrite the
            # earlier lanes' single held word before the controller samples it.
            200000000: (11,  9, _native_data_phase(11), _native_data_phase( 9), 11, 2, 3),
            233333333: (13, 10, _native_data_phase(13), _native_data_phase(10), 12, 2, 3),
            266666667: (15, 11, _native_data_phase(15), _native_data_phase(11), 12, 2, 3),
            300000000: (17, 12, _native_data_phase(17), _native_data_phase(12), 12, 3, 3),
            333333333: (19, 14, _native_data_phase(19), _native_data_phase(14), 12, 3, 3),
            366666667: (21, 16, _native_data_phase(21), _native_data_phase(16), 13, 4, 4),
            400000000: (24, 16, _native_data_phase(24), _native_data_phase(16), 14, 4, 5),
        }
        if frequency not in profiles:
            raise ValueError('Native profile supports 1600, 1866.667, 2133.333, 2400, 2666.667, 2933.333 or 3200 MT/s')
        if frequency > 333333333 and not overclock:
            raise ValueError('Native rates above 2666.667 MT/s require explicit overclock opt-in')
        cl, cwl, rdphase, wrphase, read_latency, write_latency, gate_delay = profiles[frequency]
        if latency_profile_values is not None:
            cl, cwl, rdphase, wrphase, read_latency, write_latency, gate_delay = latency_profile_values
        if is_rdimm:
            # Preserve physical serializer delay and add the RCD's command clock.
            rdphase = (-(cl + 1 + rcd_latency)) % 4
            wrphase = (-(cwl + 1 + rcd_latency)) % 4
            read_latency += math.ceil((cl + 1 + rcd_latency)/4) - math.ceil((cl + 1)/4)
            write_latency += math.ceil((cwl + 1 + rcd_latency)/4) - math.ceil((cwl + 1)/4)
        # Resolve the experiment adjustment first, then let an explicit
        # diagnostic override select the final effective PHY latency.
        read_latency = _native_effective_read_latency(
            read_latency, held_rx_return, read_latency_override)
        # The experimental high-rate profiles retain the maximum accepted
        # native delay-model parameter. Clock constraints retain the actual
        # rate: this is not a timing waiver or a claim of device compliance.
        refclk_mhz     = min(8*sys_clk_freq/1e6, 2666.666667)
        if refclk_attribute_mhz is not None:
            # Diagnostic only: AMD UG571 expects this RXTX_BITSLICE attribute
            # to match the master PLL_CLK. This override compares attributes
            # with the vendor MIG setup and does not assert datasheet correctness.
            # BITSLICE_CONTROL PLL_CLK remains the actual 1.6 GHz source.
            refclk_mhz = refclk_attribute_mhz
        self.overclock = frequency > 333333333
        pin_map        = extract_ddr_pins(platform, pads, memtype=memtype)
        validate_native_configuration(family=pin_map.family, memtype=memtype,
            databits=databits, controller_ratio='1:4')
        if queried_topology is None:
            physical, auxiliary, directory = query_device(pin_map, output_dir, vivado=vivado,
                cache_dir=query_cache_dir, force_refresh=query_force_refresh)
        else:
            queried_pins, physical, auxiliary, directory = queried_topology
            if queried_pins.fingerprint != pin_map.fingerprint or queried_pins.part != pin_map.part:
                raise ValueError('Pre-queried native topology does not match the DDR pads')
        generated = emit_core(core_module_name, physical, auxiliary, family=pin_map.family,
                              refclk_mhz=refclk_mhz, data_tbyte=data_tbyte,
                              pre_emphasis=pre_emphasis, dynamic_odelay=dynamic_odelay,
                              dqs_wrclk_monitor=with_dqs_wrclk_monitor,
                              dm_wrclk_lanes=dm_monitor_lanes)
        layout = generated.layout
        if layout.databits != databits or len(layout.lanes) != nlanes:
            raise ValueError('Queried native byte lanes do not match the requested DDR pads')
        if with_dm_wrclk_monitor and databits != 32:
            raise ValueError('DM clock diagnostic selects DM0/DM1 on x32 only')
        mapping = NativeMapping(layout, profile=dict(frequency=frequency, cl=cl, cwl=cwl,
            is_rdimm=is_rdimm, rcd_latency=rcd_latency if is_rdimm else 0, group_width=group_width,
            rdphase=rdphase, wrphase=wrphase, read_latency=read_latency,
            write_latency=write_latency, gate_delay=gate_delay, registered_tx=registered_tx,
            family=pin_map.family, with_debug=bool(with_debug),
            local_fifo_drain=bool(local_fifo_drain),
            registered_common_fifo_drain=bool(registered_common_fifo_drain),
            registered_fifo_drain=bool(registered_fifo_drain),
            read_token_fifo_drain=bool(read_token_fifo_drain),
            held_rx_return=held_rx_return,
            **({'fixed_fifo_pop': True} if fixed_fifo_pop else {}),
            **({'fixed_fifo_pop_monitor_pipeline': True}
                if fixed_fifo_pop_monitor_pipeline else {}),
            **({'read_latency_override': read_latency_override}
                if read_latency_override is not None else {}),
            **({'latency_profile': latency_profile} if latency_profile is not None else {}),
            data_tbyte=bool(data_tbyte), pre_emphasis=bool(pre_emphasis),
            dynamic_odelay=bool(dynamic_odelay),
            **({'trained_gate_delays': True} if trained_gate_delays else {}),
            **_native_fabric_vref_profile(fabric_receiver_vref),
            **({'dynamic_dci': False} if not dynamic_dci else {}),
            **({'with_rx_trace': False} if with_read_monitor and not with_rx_trace else {}),
            **({'rx_trace_lane': rx_trace_lane} if rx_trace_lane is not None else {}),
            **({'rx_boundary_monitor_format': 2} if with_rx_boundary_monitor else {}),
            **({'fifo_epoch_flush_version': 1}
                if with_scheduled_fifo_pop and read_token_fifo_drain else {})))
        self.mapping = mapping
        if fabric_receiver_vref:
            _native_fabric_vref_lanes(layout, physical, databits)
        sites        = signal_sites(layout)
        ntaps, ncontrols = mapping.tap_count, mapping.control_count
        ready_mask = (1 << ncontrols) - 1
        nbanks = len(layout.banks)
        if any(len(signal) != nbanks for signal in (pll_clk, pll_locked, pll_enable)):
            raise ValueError('Native PHY needs one local PLL clock, lock and enable per queried bank')
        lane_by_tap = {tap: lane.index for lane in layout.lanes
            for tap in lane.dq + (lane.strobe,) + (() if lane.mask is None else (lane.mask,))}
        trace_words = max(8, (8*databits + ntaps + 6 + 4*ncontrols + 16 + 31)//32)
        core = Path(output_dir).resolve() / (core_module_name + '.v')
        core.parent.mkdir(parents=True, exist_ok=True)
        core.write_text(generated.verilog)
        self.query_directory = directory
        assert riu_domain == 'riu', 'Transactional adapter requires related sys:riu 2:1 clocks'
        # The target supplies an RIU clock at half the selected sys frequency.
        # CSR address/data stay stable around the synchronized write pulse.
        # Related-clock paths remain timed; no asynchronous false paths.
        # Registers --------------------------------------------------------------------------------

        self._abi_version      = CSRStatus(32, reset=(mapping.major << 16) | mapping.minor)
        self._abi_config_id    = CSRStatus(32, reset=mapping.config_id)
        self._abi_capabilities = CSRStatus(32, reset=mapping.capabilities)
        self._rst              = CSRStorage(reset=1)
        self._en_vtc           = CSRStorage(reset=1)
        self._bisc_only        = CSRStorage(reset=0)  # Full standalone calibration on boot.
        self._debug            = CSRStatus(32)
        self._debug_clear      = CSR()
        if with_scheduled_fifo_pop:
            self._scheduled_fifo_mode = CSRStorage()
            self._scheduled_fifo_delay = CSRStorage(5, reset=7)
            self._scheduled_fifo_underflows = CSRStatus(32)
            self._scheduled_fifo_overlaps = CSRStatus(32)
            self._scheduled_fifo_missing_last = CSRStatus(nlanes)
            if read_token_fifo_drain:
                self._fifo_epoch_flush = CSR(name='fifo_epoch_flush')
                self._fifo_epoch_flush_count = CSRStatus(
                    32, name='fifo_epoch_flush_count')
        self._snapshot         = CSR()
        self._snapshot_status  = CSRStatus(32)
        self._elapsed          = CSRStatus(32)
        self._first_dly        = CSRStatus(32)
        self._first_vtc        = CSRStatus(32)
        self._faults           = CSRStatus(3)
        if with_dqs_wrclk_monitor:
            for byte in range(nlanes):
                setattr(self, f'_dqs_wrclk_edges{byte}', CSRStatus(
                    32, name=f'dqs_wrclk_edges{byte}'))
        if with_dm_wrclk_monitor:
            for lane in dm_monitor_lanes:
                setattr(self, f'_dm_wrclk_edges{lane}', CSRStatus(
                    32, name=f'dm_wrclk_edges{lane}'))
        if with_read_monitor:
            # Retain compact per-read accepted-pop diagnostics independently
            # from the optional wide waveform and read-valid snapshot CSRs.
            self._rx_lane_fault = CSRStatus()
            self._rx_lane_missing = CSRStatus(nlanes)
            self._rx_lane_reads = CSRStatus(32)
        if with_rx_boundary_monitor:
            self._rx_boundary_read_sample = CSRStorage(5)
            self._rx_boundary_read_word = CSRStorage(3)
            self._rx_boundary_read = CSR()
            self._rx_boundary_read_ack = CSRStatus()
            self._rx_boundary_done = CSRStatus()
            self._rx_boundary_start_index = CSRStatus(5)
            self._rx_boundary_pre_samples = CSRStatus(5)
            self._rx_boundary_mark_seen = CSRStatus()
            self._rx_boundary_trigger_seen = CSRStatus()
            self._rx_boundary_trigger_timeout = CSRStatus()
            self._rx_boundary_mark_cycle = CSRStatus(16)
            self._rx_boundary_word = CSRStatus(32)
            self._rx_boundary_width = CSRStatus(16)
            self._rx_boundary_depth = CSRStatus(8, reset=32)
        if with_rx_trace:
            # Capture raw Q, DFI data, FIFO status, and PHY_RDEN in an optional
            # wide ring. MRS capture and read-valid snapshot are also optional.
            from .rx_trace import NativeRXTraceLayout

            self.read_monitor_enable = Signal(reset=1)
            rx_trace_layout = NativeRXTraceLayout(databits, ntaps,
                8*databits, ncontrols=ncontrols, ca_width=40,
                mrs_address_width=24 if with_mrs_command_trace else 0,
                dqs_counter_lanes=nlanes if with_dqs_wrclk_monitor else 0,
                dm_counter_lanes=len(dm_monitor_lanes),
                launched_gate_controls=ncontrols if with_scheduled_fifo_pop else 0,
                selected_lane=rx_trace_lane)
            self.rx_trace_layout = rx_trace_layout
            rx_trace_width = ((rx_trace_layout.width + 31)//32)*32
            self._rx_trace_arm = CSR()
            if with_mrs_command_trace:
                self._rx_trace_mrs_mode = CSRStorage()
            if with_scheduled_fifo_pop:
                self._rx_trace_launch_gates = CSRStatus(16, reset=ncontrols)
            self._rx_trace_done = CSRStatus()
            self._rx_trace_index = CSRStorage(6)
            self._rx_trace_width = CSRStatus(16, reset=rx_trace_width)
            for index in range(rx_trace_width//32):
                setattr(self, '_rx_trace_word'+str(index),
                    CSRStatus(32, name='rx_trace_word'+str(index)))
            read_valid_snapshot_width = 8*databits + 2*ntaps
            self._rx_valid_snapshot_arm = CSR()
            self._rx_valid_snapshot_done = CSRStatus()
            self._rx_valid_snapshot_width = CSRStatus(16,
                reset=read_valid_snapshot_width)
            for index in range((read_valid_snapshot_width + 31)//32):
                setattr(self, '_rx_valid_snapshot_word'+str(index),
                    CSRStatus(32, name='rx_valid_snapshot_word'+str(index)))
        if with_write_monitor:
            self._probe_fast_clear = CSR()
            self._probe_fast_wren  = CSRStatus(32)
            self._probe_fast_oe    = CSRStatus(32)
            self._probe_dqs_drive  = CSRStatus(32)
            self._probe_not_ready  = CSRStatus(32)
            self._probe_first_oe_word0 = CSRStatus(32)
            self._probe_fast_command = CSRStatus(8)
            self._probe_fast_address = CSRStatus(17)
            self._probe_snapshot_valid = CSRStatus()
            self._probe_snapshot_count = CSRStatus(32)
            for index in range(6):
                setattr(self, '_probe_word'+str(index),
                    CSRStatus(32, name='probe_word'+str(index)))
            command_csr_bits = 4 * (17 + (2 + len(pads.bg)) + 7)
            write_csr_bits = 4 * (2*databits + (2*databits)//8 + 1)
            snapshot_words = 4 + (command_csr_bits + 31)//32 + (write_csr_bits + 31)//32
            for index in range(snapshot_words):
                setattr(self, '_probe_snapshot_word'+str(index),
                    CSRStatus(32, name='probe_snapshot_word'+str(index)))
        self._training_stage   = CSRStorage(8)
        self._training_error   = CSRStorage(8)
        for byte in range(nlanes):
            setattr(self, '_fifo_reads'+str(byte), CSRStatus(32, name='fifo_reads'+str(byte)))
        if fixed_fifo_pop:
            self._fifo_pop_underflows = CSRStatus(32)
            self._fifo_pop_missing = CSRStatus(nlanes)
        self._ready            = CSRStatus()
        self._dly_rdy          = CSRStatus(ncontrols)
        self._vtc_rdy          = CSRStatus(ncontrols)
        self._fifo_empty       = CSRStatus(ntaps)
        self._wlevel_en        = CSRStorage()
        self._wlevel_strobe    = CSR()
        self._dly_sel          = CSRStorage(nlanes)
        for name in ('cdly_rst', 'cdly_inc', 'rdly_dq_rst', 'rdly_dq_inc',
                     'rdly_dq_bitslip_rst', 'rdly_dq_bitslip',
                     'wdly_dq_rst', 'wdly_dq_inc', 'wdly_dqs_rst', 'wdly_dqs_inc',
                     'wdly_dq_bitslip_rst', 'wdly_dq_bitslip'):
            setattr(self, '_' + name, CSR(name=name))
        self._cdly_value         = CSRStatus(9)
        self._wdly_dqs_inc_count = CSRStatus(9)
        self._rdphase            = CSRStorage(2, reset=rdphase)
        self._wrphase            = CSRStorage(2, reset=wrphase)
        # Read-gate timing is deliberately exposed for native-PHY bring-up.
        self._gate_delay       = CSRStorage(5, reset=gate_delay)
        self._read_latency     = CSRStatus(5)
        self._riu_address      = CSRStorage(6)
        self._riu_nibble       = CSRStorage(max(1, (ncontrols-1).bit_length()))
        self._riu_wdata        = CSRStorage(16)
        self._riu_write        = CSR()
        self._riu_read         = CSR()
        self._riu_busy         = CSRStatus()
        self._riu_error        = CSRStatus()
        self._riu_rdata        = CSRStatus(16)
        self._riu_valid        = CSRStatus()
        self.software_control  = Signal()  # driven from the actual DFI owner
        self._manual_active    = CSRStatus()
        self._gate_override    = CSRStorage(nlanes)
        for byte in range(nlanes):
            setattr(self, '_gate_delay'+str(byte), CSRStorage(5,
                reset=gate_delay, name='gate_delay'+str(byte)))
        for byte in range(nlanes):
            setattr(self, '_gate_width'+str(byte), CSRStorage(4,
                reset=2, name='gate_width'+str(byte)))
        if with_read_monitor:
            # Debug-only sub-cycle gate phase. A zero phase preserves the
            # existing four-bit PHY_RDEN pattern until explicitly changed.
            for byte in range(nlanes):
                setattr(self, '_gate_phase'+str(byte), CSRStorage(2,
                    name='gate_phase'+str(byte)))
                setattr(self, '_gate_phase_upper'+str(byte), CSRStorage(2,
                    name='gate_phase_upper'+str(byte)))
        self._tap_select       = CSRStorage(max(1, (ntaps-1).bit_length()))
        self._tap_allowed      = CSRStatus()
        self._tap_status_valid = CSRStatus()
        self._tap_rx_rst       = CSR()
        self._tap_rx_inc       = CSR()
        self._tap_tx_rst       = CSR()
        self._tap_tx_inc       = CSR()
        self._tap_rx_count     = CSRStatus(9)
        self._tap_tx_count     = CSRStatus(9)
        if with_debug:
            self._trace_arm   = CSR()
            self._trace_state = CSRStatus(2)  # pending, running; done separately
            self._trace_done  = CSRStatus()
            self._trace_index = CSRStorage(6)
            for word in range(trace_words):
                setattr(self, '_trace_word'+str(word), CSRStatus(32, name='trace_word'+str(word)))
        self._fifo_lane_mode = CSRStorage()  # software-only independent lane draining
        # Burst-boundary diagnostics. Change only while software owns DFI and
        # no transaction is pending. Settings persist for controller handoff.
        self._tx_dqs_pre  = CSRStorage(8, reset=0x55)
        self._tx_dqs_post = CSRStorage(8, reset=0x55)
        self._tx_dqs_idle = CSRStorage(8, reset=0x55)
        # # #

        # Software control and DFI -----------------------------------------------------------------

        self.comb += self._manual_active.status.eq(self.software_control)
        pulse_names = ('cdly_rst', 'cdly_inc', 'rdly_dq_rst', 'rdly_dq_inc',
            'rdly_dq_bitslip_rst', 'rdly_dq_bitslip', 'wdly_dq_rst', 'wdly_dq_inc',
            'wdly_dqs_rst', 'wdly_dqs_inc', 'wdly_dq_bitslip_rst', 'wdly_dq_bitslip',
            'wlevel_strobe', 'riu_write', 'riu_read', 'tap_rx_rst', 'tap_rx_inc',
            'tap_tx_rst', 'tap_tx_inc')
        pulse                = {name: csr_cdc(getattr(self, '_' + name).wr_stb) for name in pulse_names}
        self.training_pulses = pulse
        tap_allowed = Signal()
        manual_tap = TapCommandEvents()
        self.submodules.manual_tap = manual_tap
        manual_tap_names = ('tap_rx_rst', 'tap_rx_inc', 'tap_tx_rst', 'tap_tx_inc')
        self.comb += [
            manual_tap.commands.eq(Cat(*[pulse[name] for name in manual_tap_names])),
            manual_tap.allowed.eq(tap_allowed),
        ]
        self.settings = PhySettings(phytype='USNativeDDRPHY', memtype='DDR4',
            databits=databits, dfi_databits=2*databits, nranks=1, nphases=4,
            rdphase=self._rdphase.storage, wrphase=self._wrphase.storage,
            cl=cl, cwl=cwl, cmd_latency=1 + 4*registered_tx + (rcd_latency if is_rdimm else 0),
            read_latency=read_latency, write_latency=write_latency,
            write_leveling=True, write_latency_calibration=True, read_leveling=True,
            delays=512, bitslips=8, with_dm=with_dm, strobes=nlanes)
        if is_rdimm:
            self.settings.set_rdimm(tck=1/(4*sys_clk_freq), rcd_pll_bypass=False,
                rcd_ca_cs_drive=0x5, rcd_odt_cke_drive=0x5, rcd_clk_drive=0x5)
        self.settings.usnative_mapping = mapping
        if with_rx_boundary_monitor:
            # The board-local controller drives this in sys; csr_cdc below
            # transfers the one-shot event into the fast PHY clock domain.
            self.rx_boundary_trigger = Signal()
        self.settings.tccd             = 8
        self.addressbits               = 17
        self.dfi                       = Interface(17, 2 + len(pads.bg), 1, 2*databits, 4)
        dfi                            = Interface(17, 2 + len(pads.bg), 1, 2*databits, 4)
        self.submodules += DDR4DFIMux(self.dfi, dfi)

        # Serializer boundary and delay controls ---------------------------------------------------

        ports               = {name: dict(direction=d, width=w) for name, (d, w) in
                               core_ports(layout, dqs_wrclk_monitor=with_dqs_wrclk_monitor,
                                   dm_wrclk_count=len(dm_monitor_lanes)).items()}
        signals             = {name: Signal(p['width'], name='native_' + name) for name, p in ports.items()}
        self.native_signals = signals  # simulation/bring-up probes, not extra CSRs
        rx_rst, rx_ce, tx_rst, tx_ce = [Signal(ntaps) for _ in range(4)]
        rx_rst_request, rx_ce_request = Signal(ntaps), Signal(ntaps)
        tx_rst_request, tx_ce_request = Signal(ntaps), Signal(ntaps)
        # RST_DLY is asynchronous at the native primitive. Register the
        # complete selection/decode logic, rather than exposing CSR address
        # transitions to that pin. Selection must remain stable throughout
        # the transfer so a pulse cannot reset or increment another tap.
        # RX_CLK/TX_CLK clock the variable delay controls, not serial data.
        # Hold each selection vector until its pulse crosses to the slower
        # related RIU clock. Firmware separates commands by >=100 CPU cycles;
        # callers must leave >=16 sys cycles between requests of each kind.
        selected_lane = self._dly_sel.storage != 0
        request_events = (
            (selected_lane & pulse['rdly_dq_rst']) | manual_tap.requests[0],
            (selected_lane & pulse['rdly_dq_inc']) | manual_tap.requests[1],
            pulse['cdly_rst'] | (selected_lane & (pulse['wdly_dq_rst'] | pulse['wdly_dqs_rst'])) |
                manual_tap.requests[2],
            pulse['cdly_inc'] | (selected_lane & (pulse['wdly_dq_inc'] | pulse['wdly_dqs_inc'])) |
                manual_tap.requests[3],
        )
        for request, output, event in zip(
                (rx_rst_request, rx_ce_request, tx_rst_request, tx_ce_request),
                (rx_rst, rx_ce, tx_rst, tx_ce), request_events):
            if riu_domain == 'sys':
                self.sync += output.eq(request)
            else:
                payload  = Signal(ntaps)
                transfer = PulseSynchronizer('sys', riu_domain)
                self.submodules += transfer
                self.comb += transfer.i.eq(event)
                # The event is the issue pulse for this request vector. Use
                # it to capture the vector on the same sys edge instead of
                # reducing every physical tap into a wide payload enable.
                # An event with no selected tap safely captures zero.
                self.sync += If(event, payload.eq(request))
                getattr(self.sync, riu_domain).__iadd__(output.eq(Mux(transfer.o, payload, 0)))
        rx_count, tx_count = Signal(9*ntaps), Signal(9*ntaps)
        self.delay_controls = dict(rx_rst=rx_rst, rx_ce=rx_ce, tx_rst=tx_rst, tx_ce=tx_ce)
        fifo_empty, dqs_data = Signal(ntaps), Signal(8*nlanes)
        self.fifo_empty_input = fifo_empty
        data_tristate         = Signal(reset=1)
        slice_vtc             = Signal(reset=1)
        self.slice_vtc        = slice_vtc
        kwargs = {('i_' if p['direction'] == 'input' else 'o_') + name: signals[name]
                  for name, p in ports.items()}
        kwargs.update(i_i_delay_clk=ClockSignal(riu_domain), i_i_rx_delay_rst=rx_rst, i_i_rx_delay_ce=rx_ce,
            i_i_tx_delay_rst=tx_rst, i_i_tx_delay_ce=tx_ce,
            o_o_rx_delay_count=rx_count, o_o_tx_delay_count=tx_count,
            o_o_fifo_empty=fifo_empty, i_i_dqs_tx_data=dqs_data,
            i_i_data_tristate=data_tristate, i_i_slice_en_vtc=slice_vtc)
        # Register the final serializer inputs, after CA packing and TX
        # bitslip muxes. Move command, data, strobe, output enables and read
        # gate by the same sys cycle. Their relative wire timing is preserved;
        # received data and rddata_valid arrive one sys cycle later.
        # CK and BISC/reset/RIU sequencing are not transaction pipelines.
        self.tx_launch_inputs, self.tx_launch_outputs = {}, {}
        for name in signals:
            if ((name.endswith('_tx_data') and name != 'i_ck_t_tx_data')
                    or name in ('i_data_tbyte', 'i_cmd_tbyte', 'i_phy_rden')):
                self.tx_launch_inputs[name] = signals[name]
        self.tx_launch_inputs.update(i_dqs_tx_data=dqs_data, i_data_tristate=data_tristate)
        for name, source in self.tx_launch_inputs.items():
            if registered_tx:
                reset = ((1 << len(source)) - 1 if name in
                         ('i_cs_n_tx_data', 'i_act_n_tx_data', 'i_data_tristate') else 0)
                output = Signal(len(source), reset=reset, name='native_launch_' + name)
                output.attr.add('dont_touch')
                self.sync += output.eq(source)
            else:
                output = source
            self.tx_launch_outputs[name] = output
            kwargs['i_' + name] = output
        riu_data, riu_valid = connect_core(self, generated, physical, kwargs,
            module_name=core_module_name)
        if with_dqs_wrclk_monitor:
            from .clock_monitor import NativeDQSClockMonitor

            self.submodules.dqs_wrclk_monitor = monitor = NativeDQSClockMonitor(
                [signals['o_dqs_wrclk'][byte] for byte in range(nlanes)])
            for byte in range(nlanes):
                # The stopped external DQS clock is divided to one BL8 word
                # per application cycle. Constrain the opt-in observation
                # counters at that maximum rate; Gray counters synchronize
                # into sys independently of the returning strobe phase.
                monitor_clock = monitor.source_clocks[byte]
                platform.add_period_constraint(monitor_clock, 1e9/sys_clk_freq)
                self.comb += getattr(self, f'_dqs_wrclk_edges{byte}').status.eq(
                    monitor.counts[byte])
        if with_dm_wrclk_monitor:
            self.submodules.dm_wrclk_monitor = dm_monitor = NativeDQSClockMonitor(
                [signals['o_dm_wrclk'][index]
                    for index in range(len(dm_monitor_lanes))],
                domain_prefix='usnative_dm_wrclk', signal_prefix='dm_wrclk')
            for index, lane in enumerate(dm_monitor_lanes):
                self.comb += getattr(self, f'_dm_wrclk_edges{lane}').status.eq(
                    dm_monitor.counts[index])
        platform.add_source(str(core))

        # Control runs in the DFI domain; implementation checks its timing.
        # Reset release is deterministic: clocks off, reset asserted, reset
        # removed, 64 settling cycles, then enable CLKOUTPHY and await BISC.
        count = Signal(8)
        phy_reset, ctrl_reset = Signal(reset=1), Signal(reset=1)
        locked = Signal()
        self.specials += MultiReg(reduce(and_, (pll_locked[bank] for bank in range(nbanks))), locked)
        self.sync += If(self._rst.storage | ~locked,
            count.eq(0), phy_reset.eq(1), ctrl_reset.eq(1), pll_enable.eq(0)
        ).Else(
            If(count != 255, count.eq(count + 1)),
            If(count == 63, phy_reset.eq(0), ctrl_reset.eq(0)),
            If(count == 127, pll_enable.eq((1 << nbanks) - 1)))
        ready       = Signal()
        initialized = Signal()
        dly_ready, vtc_ready = Signal(ncontrols), Signal(ncontrols)
        self.specials += MultiReg(signals['o_dly_rdy'], dly_ready), MultiReg(signals['o_vtc_rdy'], vtc_ready)
        riu_reset, riu_vtc = Signal(reset=1), Signal()
        vtc_request = Signal()
        if riu_domain == 'sys':
            self.comb += riu_reset.eq(ctrl_reset)
            self.sync += riu_vtc.eq(vtc_request)
        else:
            self.sync.riu += riu_reset.eq(ctrl_reset)
            # UG571 native bring-up requires two RIU_CLK synchronizer stages
            # before enabling control VT tracking after DLY_RDY. The request
            # is a persistent level; tap commands wait for it to settle.
            self.specials += MultiReg(vtc_request, riu_vtc, odomain='riu')
        self.submodules.riu_transaction = transaction = RIUTransaction(ncontrols, mapping.riu_indices)
        self.submodules.riu_launch = launch = RIUFallingLaunch(transaction, platform)
        self.comb += [
            transaction.request.eq(pulse['riu_write'] | pulse['riu_read']),
            transaction.write.eq(pulse['riu_write']), transaction.reset.eq(ctrl_reset | ~ready),
            transaction.address.eq(self._riu_address.storage),
            transaction.select.eq(self._riu_nibble.storage),
            transaction.wdata.eq(self._riu_wdata.storage),
            transaction.native_rdata.eq(riu_data), transaction.native_valid.eq(riu_valid),
            self._riu_busy.status.eq(transaction.busy), self._riu_error.status.eq(transaction.error)]
        self.sync += If(self._rst.storage | ~locked, initialized.eq(0)).Elif(
            pll_enable & (dly_ready == ready_mask) & (vtc_ready == ready_mask), initialized.eq(1))
        # Register readiness before high-fanout training/PHY control.
        self.sync += ready.eq(initialized & locked & (dly_ready == ready_mask) & ~self._rst.storage)
        self.comb += [
            self._ready.status.eq(ready), self._dly_rdy.status.eq(dly_ready),
            self._vtc_rdy.status.eq(vtc_ready),
            signals['i_div_clk'].eq(ClockSignal()), signals['i_riu_clk'].eq(ClockSignal(riu_domain)),
            signals['i_pll_clk'].eq(pll_clk), signals['i_rst'].eq(riu_reset),
            signals['i_clb2phy_tristate_odelay_rst'].eq(riu_reset),
            vtc_request.eq(self._en_vtc.storage & pll_enable & (dly_ready == ready_mask)),
            slice_vtc.eq(~initialized | self._en_vtc.storage),
            signals['i_en_vtc'].eq(riu_vtc),
            signals['i_riu_addr'].eq(launch.address),
            signals['i_riu_wr_data'].eq(launch.wdata),
            signals['i_riu_nibble_sel'].eq(launch.select),
            signals['i_riu_wr_en'].eq(launch.write),
            self._riu_rdata.status.eq(transaction.rdata),
            self._riu_valid.status.eq(transaction.valid),
            # UG571: controller TBYTE_IN is active-high output enable;
            # TX_BITSLICE_TRI inverts it into active-high buffer tristate.
            # Keep zero throughout reset, including TX_OUTPUT_PHASE_90 reset.
            signals['i_cmd_tbyte'].eq(Mux(ready, 15, 0)),
            pads.reset_n.eq(dfi.phases[0].reset_n & ready & ~self._bisc_only.storage),
        ]
        # FIFO status is telemetry, not the live drain control. Capture it
        # before the wide CSR read mux to keep that path out of I/O routing.
        self.sync += self._fifo_empty.status.eq(fifo_empty)
        # Status remains accessible with native clocks disabled. Snapshot and
        # fault bits survive software PHY reset; only debug_clear clears them.
        elapsed = Signal(32)
        first_dly, first_vtc = Signal(32), Signal(32)
        faults = Signal(3)
        previous_lock, previous_dly, previous_vtc = Signal(), Signal(), Signal()
        status = Cat(locked, pll_enable, phy_reset, ctrl_reset, slice_vtc,
            riu_vtc, initialized, ready, dly_ready, vtc_ready, count)
        self.comb += [self._debug.status.eq(status), self._elapsed.status.eq(elapsed),
            self._first_dly.status.eq(first_dly), self._first_vtc.status.eq(first_vtc),
            self._faults.status.eq(faults)]
        self.sync += [
            previous_lock.eq(locked), previous_dly.eq(dly_ready == ready_mask),
            previous_vtc.eq(vtc_ready == ready_mask),
            If(self._rst.storage, elapsed.eq(0)).Elif(elapsed != 0xffffffff, elapsed.eq(elapsed + 1)),
            If(self._debug_clear.wr_stb,
                first_dly.eq(0), first_vtc.eq(0), faults.eq(0), self._snapshot_status.status.eq(0)
            ).Else(
                If((first_dly == 0) & (dly_ready == ready_mask), first_dly.eq(elapsed)),
                If((first_vtc == 0) & (vtc_ready == ready_mask), first_vtc.eq(elapsed)),
                If(previous_lock & ~locked, faults[0].eq(1)),
                If(previous_dly & (dly_ready != ready_mask) & ~self._rst.storage, faults[1].eq(1)),
                If(previous_vtc & (vtc_ready != ready_mask) & self._en_vtc.storage & ~self._rst.storage, faults[2].eq(1)),
                If(self._snapshot.wr_stb | (previous_lock & ~locked), self._snapshot_status.status.eq(status)))]
        # CK is 01010101, so its rising edges are slots 0,2,4,6. Change CA
        # on the preceding falling edge: p0 at slots 1/2, p1 at 3/4, etc.
        # This deliberately adds one CK of command latency. Slot 0 holds
        # previous-cycle p3; slot 7 carries current-cycle p3 across the boundary.
        ca = {'adr': ('address', 17), 'ba': ('bank', 2), 'bg': ('bank', len(pads.bg)),
              'cs_n': ('cs_n', 1), 'cke': ('cke', 1), 'odt': ('odt', 1), 'act_n': ('act_n', 1)}
        for name, (field, width) in ca.items():
            for bit in range(width):
                values = [(getattr(p, ('we_n', 'cas_n', 'ras_n')[bit-14])[0]
                    if name == 'adr' and bit >= 14 else getattr(p, field)[bit + (2 if name == 'bg' else 0)])
                    for p in dfi.phases]
                previous = Signal(reset=1 if name in ('cs_n', 'act_n') else 0)
                self.sync += previous.eq(values[3])
                self.comb += signals['i_'+name+'_tx_data'][8*bit:8*bit+8].eq(
                    Cat(previous, values[0], values[0], values[1], values[1], values[2], values[2], values[3]))
            pad = Cat(pads.a, pads.we_n, pads.cas_n, pads.ras_n) if name == 'adr' else getattr(pads, name)
            self.comb += pad.eq(signals['o_'+name+'_serial_out'])
        self.specials += Instance('OBUFDS', i_I=signals['o_ck_t_serial_out'], o_O=pads.clk_p, o_OB=pads.clk_n)

        wr = TappedDelayLine(reduce(or_, [p.wrdata_en for p in dfi.phases]), ntaps=6)
        read_command = reduce(or_, [p.rddata_en for p in dfi.phases])
        if fixed_fifo_pop:
            fixed_ready = ready & (vtc_ready == ready_mask)
            rd = NativeFixedReadTappedDelayLine(
                read_command, ntaps=32, flush=self._rst.storage | ~fixed_ready)
        else:
            rd = TappedDelayLine(read_command, ntaps=32)
        self.submodules += wr, rd
        write_data = [phase.wrdata for phase in dfi.phases]
        write_mask = [phase.wrdata_mask for phase in dfi.phases]
        data_pipeline_cycles = (write_latency + 1 - write_data_advance
            if pipeline_write_data else 0)
        if write_data_pipeline_cycles is not None:
            data_pipeline_cycles = write_data_pipeline_cycles
        if data_pipeline_cycles:
            # Align data and masks with DQS without holding/repeating a burst.
            # The reduced-width converter has separate experimental launch
            # settings. Full-rate launch must also account for command/CWL
            # timing at the memory; matching the internal OE tap alone does
            # not establish physical DQ/DM alignment.
            for phase in range(len(dfi.phases)):
                data_delay = TappedDelayLine(write_data[phase],
                    ntaps=data_pipeline_cycles)
                mask_delay = TappedDelayLine(write_mask[phase],
                    ntaps=data_pipeline_cycles)
                setattr(self.submodules, 'wrdata_delay'+str(phase), data_delay)
                setattr(self.submodules, 'wrmask_delay'+str(phase), mask_delay)
                write_data[phase] = data_delay.output
                write_mask[phase] = mask_delay.output
        # Profile write latency selects the DFI enable tap. It increases at
        # higher rates to account for the additional controller pipeline.
        oe = wr.taps[write_latency]
        pre, post = wr.taps[write_latency-1] & ~oe, wr.taps[write_latency+1] & ~oe
        dqs_oe = oe | pre | post | self._wlevel_en.storage
        # T_OUT -> IOB.T is a dedicated route. DQ/DM use the native T input
        # with a registered drive window; write leveling releases DQ/DM while
        # DQS continues to use the native TX_BITSLICE_TRI serialized control.
        self.submodules.data_drive_delay = data_drive = TappedDelayLine(oe | pre | post, ntaps=1)
        self.comb += data_tristate.eq(~ready | self._wlevel_en.storage | ~data_drive.output)
        # Match the working USDDRPHY: its normal DQS pattern continues
        # toggling throughout the enable window. Connecting the optional
        # pre/post patterns inserts gaps that can enter the write burst as
        # native output delay/bitslip changes. Keep WL's single pulse.
        self.submodules.dqs_pattern = pattern = DQSPattern(
            wlevel_en=self._wlevel_en.storage, wlevel_strobe=pulse['wlevel_strobe'])
        selected_pattern = Signal(8)
        self.tx_pattern  = selected_pattern
        self.tx_window   = dict(oe=oe, pre=pre, post=post)
        self.comb += selected_pattern.eq(Mux(self._wlevel_en.storage, pattern.o,
            Mux(oe, 0x55, Mux(post, self._tx_dqs_post.storage,
                Mux(pre, self._tx_dqs_pre.storage, self._tx_dqs_idle.storage)))))
        # Register TBYTE at its final driver: an OR across advancing taps
        # can glitch during pre/active/post handoffs. Computing one cycle
        # earlier preserves the ordinary write window exactly.
        dqs_drive      = Signal()
        self.dqs_drive = dqs_drive
        self.sync += dqs_drive.eq(ready & (wr.taps[write_latency-2] | wr.taps[write_latency-1] |
            wr.taps[write_latency] | self._wlevel_en.storage))
        self.comb += signals['i_data_tbyte'].eq(Replicate(dqs_drive, 4*ncontrols))
        if with_write_monitor:
            # Capture every DFI phase atomically at write-data enable, plus
            # four preceding command cycles. The prior monitor paired the
            # event with phase-3 command and phase-0 data from later cycles.
            clear = csr_cdc(self._probe_fast_clear.wr_stb)
            fast_wren = reduce(or_, [phase.wrdata_en for phase in dfi.phases])
            self.submodules.write_snapshot = write_snapshot = NativeWriteSnapshot(dfi.phases)
            self.comb += [write_snapshot.trigger.eq(fast_wren), write_snapshot.clear.eq(clear),
                self._probe_snapshot_valid.status.eq(write_snapshot.valid),
                self._probe_snapshot_count.status.eq(write_snapshot.count),
                self._probe_fast_command.status.eq(write_snapshot.command),
                self._probe_fast_address.status.eq(write_snapshot.address)]
            snapshot_words = [getattr(self, '_probe_snapshot_word'+str(index))
                for index in range(len(write_snapshot.words))]
            legacy_words = [getattr(self, '_probe_word'+str(index)) for index in range(6)]
            for index, csr in enumerate(snapshot_words):
                self.comb += csr.status.eq(write_snapshot.words[index])
            # Keep the original six readout names as aliases for the first
            # six words, so old diagnostics can still dump the new format.
            for index, csr in enumerate(legacy_words):
                self.comb += csr.status.eq(write_snapshot.words[index])
            self.sync += If(clear,
                self._probe_fast_wren.status.eq(0),
                self._probe_fast_oe.status.eq(0),
                self._probe_dqs_drive.status.eq(0),
                self._probe_not_ready.status.eq(0),
                self._probe_first_oe_word0.status.eq(0),
            ).Else(
                If(fast_wren,
                    self._probe_fast_wren.status.eq(self._probe_fast_wren.status + 1),
                ),
                If(oe,
                    self._probe_fast_oe.status.eq(self._probe_fast_oe.status + 1),
                    If(self._probe_fast_oe.status == 0,
                        self._probe_first_oe_word0.status.eq(dfi.phases[0].wrdata[:32]))),
                If(dqs_drive, self._probe_dqs_drive.status.eq(
                    self._probe_dqs_drive.status + 1)),
                If((fast_wren | oe) & ~ready, self._probe_not_ready.status.eq(
                    self._probe_not_ready.status + 1)),
            )
        # A read request opens the native gate for a burst plus margins. The
        # gate position must be trained; it is not assumed to equal IOSERDES.
        for byte in range(nlanes):
            selected = self._gate_override.storage[byte]
            delay = Signal(5)
            width = Signal(4)
            phase = Signal(2)
            upper_phase = Signal(2)
            # Decode software gate settings at the PHY clock boundary. The
            # settings are changed between training probes, so one PHY cycle
            # of latency is harmless and keeps the CSR clock out of the gate
            # counter's timing-critical feedback path.
            lane_delay, lane_width = _native_gate_settings(
                self.software_control, trained_gate_delays, selected,
                getattr(self, '_gate_delay'+str(byte)).storage,
                self._gate_delay.storage,
                getattr(self, '_gate_width'+str(byte)).storage)
            self.sync += [
                delay.eq(lane_delay),
                width.eq(lane_width),
            ]
            if with_read_monitor:
                self.sync += [
                    phase.eq(getattr(self, '_gate_phase'+str(byte)).storage),
                    upper_phase.eq(getattr(self, '_gate_phase_upper'+str(byte)).storage),
                ]
            # A BL8 burst spans one fabric cycle. Hardware MPR tests show
            # the former two-cycle window admits unwanted trailing edges.
            gate      = Array(rd.taps)[delay]
            remaining = Signal(4)
            self.sync += If(self._rst.storage, remaining.eq(0)).Elif(gate,
                remaining.eq(Mux(width > 1, Mux(width > 8, 7, width-1), 0))
            ).Elif(remaining != 0, remaining.eq(remaining-1))
            active = Signal()
            previous_active = Signal()
            self.comb += active.eq(_native_gate_active(
                gate, remaining, self._wlevel_en.storage, ready, self._rst.storage))
            self.sync += previous_active.eq(active)
            # Each PHY_RDEN bit covers one quarter of a PHY cycle. Carry the
            # shifted tail into the next cycle, as MIG's rdEn shift register
            # does; a phase of zero is identical to the original gate.
            for control_index, control in enumerate(layout.lanes[byte].controls):
                control_phase = phase if control_index == 0 else upper_phase
                current_mask = Array([15, 14, 12, 8])[control_phase]
                carry_mask = Array([0, 1, 3, 7])[control_phase]
                self.comb += signals['i_phy_rden'][4*control:4*control+4].eq(
                    (Replicate(active, 4) & current_mask) |
                    (Replicate(previous_active, 4) & carry_mask))
        for control in set(range(ncontrols)) - set(mapping.data_controls):
            self.comb += signals['i_phy_rden'][4*control:4*control+4].eq(0)
        # Native FIFOs must not be read past empty when DQS stops between
        # bursts. A registered inverted EMPTY can overrun by one word and
        # repeatedly expose old contents. Drain only when every DQ FIFO has
        # a word, aligning the independent DQS domains at this interface.
        lane_available = []
        for byte in range(nlanes):
            empty = reduce(or_, [fifo_empty[sites[f'o_dq_serial_out[{i}]']]
                                for i in range(group_width*byte, group_width*byte+group_width)])
            lane_available.append(~empty)
        common_available = reduce(and_, lane_available)
        fixed_pop_request = None
        if fixed_fifo_pop:
            fixed_pop_request = _native_fixed_fifo_pop_request(
                rd.taps, read_latency,
                ready & (vtc_ready == ready_mask), self._rst.storage)
            fixed_pop_monitor = NativeFixedFIFOPopMonitor(nlanes,
                pipeline=fixed_fifo_pop_monitor_pipeline)
            self.submodules.fixed_fifo_pop_monitor = fixed_pop_monitor
            lane_available_vector = Cat(*lane_available)
            self.comb += [
                fixed_pop_monitor.pop.eq(fixed_pop_request),
                fixed_pop_monitor.available.eq(lane_available_vector),
                fixed_pop_monitor.clear.eq(self._debug_clear.wr_stb),
                fixed_pop_monitor.reset.eq(self._rst.storage),
                self._fifo_pop_underflows.status.eq(fixed_pop_monitor.underflows),
                self._fifo_pop_missing.status.eq(fixed_pop_monitor.missing),
            ]
        registered_lane_drains = None
        if registered_fifo_drain:
            registered_lane_drains = _registered_fifo_lane_drains(
                self, lane_available, ready, self._rst.storage)
        scheduled_mode = Constant(0)
        scheduled_return = None
        scheduled_control = None
        epoch_flush_enabled = with_scheduled_fifo_pop and read_token_fifo_drain
        token_idle = Signal(name='fifo_read_token_idle') if epoch_flush_enabled else None
        epoch_flush_accept = Constant(0)
        epoch_flush_read_enable = Constant(0, ntaps)
        if with_scheduled_fifo_pop:
            if with_scheduled_fifo_return:
                from .scheduled_return import ScheduledNativeReturn
            else:
                from .scheduled_pop import ScheduledFIFOPop
            scheduled_mode = self.software_control & self._scheduled_fifo_mode.storage
            if with_scheduled_fifo_return:
                self.submodules.scheduled_fifo_return = scheduled_return = \
                    ScheduledNativeReturn(lanes=nlanes, data_width=8*databits)
                scheduled_control = scheduled_return
                self.comb += scheduled_return.fifo_front.eq(signals['o_dq_rx_data'])
            else:
                self.submodules.scheduled_fifo_pop = scheduled_pop = ScheduledFIFOPop(nlanes)
                scheduled_control = scheduled_pop
            self.comb += [
                scheduled_control.read_request.eq(rd.input),
                scheduled_control.delay.eq(self._scheduled_fifo_delay.storage),
                scheduled_control.ready.eq(ready),
                scheduled_control.reset.eq(self._rst.storage | ~scheduled_mode),
                scheduled_control.fifo_empty.eq(Cat(*[~available for available in lane_available])),
            ]
            self.sync += If(self._debug_clear.wr_stb,
                self._scheduled_fifo_underflows.status.eq(0),
                self._scheduled_fifo_overlaps.status.eq(0),
                self._scheduled_fifo_missing_last.status.eq(0),
            ).Else(
                If(scheduled_control.underflow,
                    self._scheduled_fifo_underflows.status.eq(
                        self._scheduled_fifo_underflows.status + 1),
                    self._scheduled_fifo_missing_last.status.eq(scheduled_control.missing_lanes)),
                If(scheduled_control.overlap,
                    self._scheduled_fifo_overlaps.status.eq(
                        self._scheduled_fifo_overlaps.status + 1)),
            )
        if epoch_flush_enabled:
            epoch_flush_request = csr_cdc(self._fifo_epoch_flush.wr_stb)
            epoch_flush_accept, epoch_flush_read_enable = _native_fifo_epoch_flush(
                self, epoch_flush_request, self.software_control, ready,
                self._rst.storage, scheduled_mode, token_idle, fifo_empty,
                mapping.dq_taps + mapping.dqs_taps + mapping.dm_taps,
                self._fifo_epoch_flush_count.status)
        token_lane_drains = None
        token_lane_pops = None
        if read_token_fifo_drain:
            token_lane_drains, token_lane_pops = _read_token_fifo_lane_drains(
                self, lane_available, rd.input,
                rd.taps[self.settings.read_latency-1], ready,
                self._rst.storage | scheduled_mode,
                idle_status=token_idle,
                epoch_flush=epoch_flush_accept if epoch_flush_enabled else None)
        lane_aligned_available = _lane_aligned_fifo_available(self, lane_available)
        if registered_common_fifo_drain:
            common_available_registered = Signal()
            self.sync += common_available_registered.eq(common_available)
            common_available = common_available_registered
        for byte in range(nlanes):
            drain = Signal()
            reads = getattr(self, '_fifo_reads'+str(byte)).status
            # Independent lane draining is a training diagnostic. Leave its
            # software mux out of the production FIFO_EMPTY timing path.
            if fixed_fifo_pop:
                # MIG-like fixed read-return scheduling: one common pop pulse
                # reaches every lane's DQ/DQS/DM FIFO. EMPTY is telemetry only.
                self.comb += drain.eq(fixed_pop_request)
            elif read_token_fifo_drain:
                if with_scheduled_fifo_pop:
                    scheduled_rd_en = (scheduled_return.rd_en if scheduled_return is not None
                        else scheduled_control.rd_en)
                    self.comb += drain.eq(Mux(scheduled_mode,
                        scheduled_rd_en, token_lane_drains[byte]))
                else:
                    self.comb += drain.eq(token_lane_drains[byte])
            elif registered_fifo_drain:
                self.comb += drain.eq(registered_lane_drains[byte])
            elif local_fifo_drain:
                availability = lane_available[byte]
            elif with_debug:
                availability = Mux(self.software_control & self._fifo_lane_mode.storage,
                    lane_available[byte],
                    common_available if registered_common_fifo_drain
                    else lane_aligned_available[byte])
            else:
                availability = (common_available if registered_common_fifo_drain
                    else lane_aligned_available[byte])
            if not (fixed_fifo_pop or registered_fifo_drain or read_token_fifo_drain):
                self.comb += drain.eq(ready & availability)
            if fixed_fifo_pop:
                # Do not report an empty-FIFO strobe as a fresh data word.
                counted_pop = drain & lane_available[byte]
            else:
                counted_pop = token_lane_pops[byte] if read_token_fifo_drain else drain
            if with_scheduled_fifo_pop:
                counted_pop = Mux(scheduled_mode, drain & lane_available[byte], counted_pop)
            self.sync += If(self._debug_clear.wr_stb, reads.eq(0)).Elif(counted_pop, reads.eq(reads+1))
            lane = layout.lanes[byte]
            for tap in lane.dq + (lane.strobe,) + (() if lane.mask is None else (lane.mask,)):
                epoch_pop = (epoch_flush_read_enable[tap]
                    if epoch_flush_enabled else Constant(0))
                self.comb += signals['i_fifo_rd_en'][tap].eq((drain & ready) | epoch_pop)
        for tap in set(range(ntaps)) - set(lane_by_tap):
            self.comb += signals['i_fifo_rd_en'][tap].eq(0)

        for byte in range(nlanes):
            slip = BitSlip(8, i=selected_pattern, rst=self._rst.storage |
                (self._dly_sel.storage[byte] & pulse['wdly_dq_bitslip_rst']),
                slp=self._dly_sel.storage[byte] & pulse['wdly_dq_bitslip'])
            self.submodules += slip
            self.comb += dqs_data[8*byte:8*byte+8].eq(slip.o)
            self.specials += Instance('IOBUFDS', i_I=signals['o_dqs_t_serial_out'][byte],
                i_T=signals['o_dqs_t_tristate'][byte],
                o_O=signals['i_dqs_t_serial_in'][byte], io_IO=pads.dqs_p[byte], io_IOB=pads.dqs_n[byte])
        dq_bitslip_outputs, dq_bitslip_states = [], []
        _add_native_data_iobufs(self, pads, signals, layout, physical,
            databits=databits, dynamic_dci=dynamic_dci,
            fabric_receiver_vref=fabric_receiver_vref)
        for name, width, padname in ([('dq', databits, 'dq')] + ([('dm_n', databits//8, 'dm')] if with_dm else [])):
            for bit in range(width):
                byte = bit//group_width if name == 'dq' else bit
                y    = sites[f'o_{name}_serial_out[{bit}]']
                dyn  = bit if name == 'dq' else databits + bit
                data = Cat(*[(write_data[s//2][(s%2)*databits+bit] if name=='dq'
                    else ~write_mask[s//2][(s%2)*(databits//8)+bit]) for s in range(8)])
                tx = BitSlip(8, i=data, rst=self._rst.storage |
                    (self._dly_sel.storage[byte] & pulse['wdly_dq_bitslip_rst']),
                    slp=self._dly_sel.storage[byte] & pulse['wdly_dq_bitslip'])
                self.submodules += tx
                self.comb += signals[f'i_{name}_tx_data'][8*bit:8*bit+8].eq(tx.o)
                if name == 'dq':
                    rx = NativeRXBitslip(i=signals['o_dq_rx_data'][8*bit:8*bit+8],
                        rst=self._rst.storage | (self._dly_sel.storage[byte] & pulse['rdly_dq_bitslip_rst']),
                        slp=self._dly_sel.storage[byte] & pulse['rdly_dq_bitslip'],
                        held_return=held_rx_return,
                        accepted=(signals['i_fifo_rd_en'][y] & ~fifo_empty[y]
                                  if held_rx_return is not None else None),
                        captured_word=(scheduled_return.captured_word[8*bit:8*bit+8]
                            if scheduled_return is not None else None),
                        captured_select=(scheduled_mode
                            if scheduled_return is not None else None))
                    self.submodules += rx
                    dq_bitslip_outputs.append(rx.o)
                    dq_bitslip_states.append(rx.shift)
                    for s in range(8):
                        self.comb += dfi.phases[s//2].rddata[(s%2)*databits+bit].eq(rx.o[s])
        # signal_sites() enumerates every slice exactly once. A range check
        # replaces a wide OR of one equality comparator per physical tap.
        if set(sites.values()) != set(range(ntaps)):
            raise ValueError('Native tap IDs must cover the queried slice range')
        # The manual-command acceptance gate shares the fast PHY domain with
        # its one-hot selector. Register validity at that boundary as well;
        # otherwise the CSR comparison feeds every payload through the
        # command pulse despite the staged selector.
        tap_valid = Signal()
        self.sync += tap_valid.eq(self._tap_select.storage < ntaps)
        self.comb += tap_allowed.eq(self.software_control & ready &
            ~self._en_vtc.storage & tap_valid)
        self.comb += self._tap_allowed.status.eq(tap_allowed)
        # Every delay update and selector change invalidates the settled view.
        previous_select = Signal(len(self._tap_select.storage))
        self.sync += previous_select.eq(self._tap_select.storage)
        # Invalidate on issued commands, including a command whose selection
        # is rejected. Reducing all physical request bits here creates a wide
        # combinational path from the selector CSR into both status trees.
        tap_commands = [pulse[name] for name in (
            'cdly_rst', 'cdly_inc', 'rdly_dq_rst', 'rdly_dq_inc',
            'wdly_dq_rst', 'wdly_dq_inc', 'wdly_dqs_rst', 'wdly_dqs_inc')]
        status_change_names = (
            'cdly_rst', 'cdly_inc', 'rdly_dq_rst', 'rdly_dq_inc',
            'wdly_dq_rst', 'wdly_dq_inc', 'wdly_dqs_rst', 'wdly_dqs_inc',
            'tap_rx_rst', 'tap_rx_inc', 'tap_tx_rst', 'tap_tx_inc')
        source_status_change = reduce(or_, [
            getattr(self, '_' + name).wr_stb for name in status_change_names] +
            [self._tap_select.re])
        status_change = ((previous_select != self._tap_select.storage) |
            reduce(or_, tap_commands) | manual_tap.invalidate)
        status_valid = []
        for kind, source in (('rx', rx_count), ('tx', tx_count)):
            tree = RegisteredTapStatus(ntaps)
            setattr(self.submodules, 'tap_status_' + kind, tree)
            self.comb += [tree.source.eq(source), tree.select.eq(self._tap_select.storage),
                tree.change.eq(status_change), tree.ready.eq(ready & ~ctrl_reset & tap_valid),
                getattr(self, '_tap_' + kind + '_count').status.eq(csr_status_cdc(tree.value))]
            status_valid.append(tree.csr_valid)
        settled = status_valid[0] & status_valid[1]
        self.comb += self._tap_status_valid.status.eq(
            csr_status_cdc(settled, invalidation=status_change,
                source_invalidation=source_status_change))
        # The selector CSR is on the controller clock. Decode it once at the
        # PHY clock before fanning out to the manual delay-command payloads.
        # Firmware leaves the selector stable between separate CSR commands.
        manual_select = Signal(ntaps)
        for y in range(ntaps):
            self.sync += manual_select[y].eq(self._tap_select.storage == y)
        for name, y in sites.items():
            byte     = lane_by_tap.get(y)
            selected = self._dly_sel.storage[byte] if byte is not None else 0
            is_dqs   = 'dqs_t' in name
            is_data  = '_dq_' in name or '_dm_n_' in name
            manual   = manual_select[y]
            self.comb += [
                rx_rst_request[y].eq((selected & pulse['rdly_dq_rst'] if is_data else 0) | (manual & manual_tap.requests[0])),
                rx_ce_request[y].eq((selected & pulse['rdly_dq_inc'] if is_data else 0) | (manual & manual_tap.requests[1])),
                tx_rst_request[y].eq((selected & (pulse['wdly_dqs_rst'] if is_dqs else pulse['wdly_dq_rst'])
                    if is_data or is_dqs else pulse['cdly_rst']) | (manual & manual_tap.requests[2])),
                tx_ce_request[y].eq((selected & (pulse['wdly_dqs_inc'] if is_dqs else pulse['wdly_dq_inc'])
                    if is_data or is_dqs else pulse['cdly_inc']) | (manual & manual_tap.requests[3]))]
        for y in set(range(ntaps)) - set(sites.values()):
            self.comb += [rx_rst_request[y].eq(0), rx_ce_request[y].eq(0), tx_rst_request[y].eq(0), tx_ce_request[y].eq(0)]
        dqs_counts = [tx_count[9*sites[f'o_dqs_t_serial_out[{b}]']:9*sites[f'o_dqs_t_serial_out[{b}]']+9]
                      for b in range(nlanes)]
        selected_count = dqs_counts[0]
        for byte in range(1, nlanes):
            selected_count = Mux(self._dly_sel.storage[byte], dqs_counts[byte], selected_count)
        self.sync += self._wdly_dqs_inc_count.status.eq(selected_count)
        ck = sites['o_ck_t_serial_out[0]']
        self.sync += self._cdly_value.status.eq(tx_count[9*ck:9*ck+9])
        valid = rd.taps[self.settings.read_latency-1]
        if fixed_fifo_pop:
            valid = valid & ~self._rst.storage & (vtc_ready == ready_mask)
        read_valid = valid
        if scheduled_return is not None:
            # Keep a tag aligned with the original fixed-valid edge. If a
            # scheduled request is cancelled by reset, READY or mode-off,
            # suppress that command's later fixed-valid fallback as well.
            scheduled_fixed_tag = TappedDelayLine(
                rd.input & scheduled_mode, ntaps=self.settings.read_latency)
            self.submodules.scheduled_fixed_tag = scheduled_fixed_tag
            scheduled_fixed_mask = scheduled_fixed_tag.taps[
                self.settings.read_latency-1]
            # This alternate valid is available only in explicit software
            # ownership with scheduled FIFO mode enabled. Hardware owner and
            # every default build retain command-relative timing; the PHY
            # setting/read-latency CSR remains the fixed controller contract.
            read_valid = Mux(scheduled_mode, scheduled_return.valid,
                valid & ~scheduled_fixed_mask)
        for p in dfi.phases:
            self.comb += p.rddata_valid.eq((read_valid & ready) | self._wlevel_en.storage)
        self.comb += self._read_latency.status.eq(self.settings.read_latency)

        if with_read_monitor:
            from .rx_trace import NativeRXFIFOStatusTrace, NativeRXLanePopScoreboard

            self.submodules.rx_fifo_status_trace = fifo_status_trace = \
                NativeRXFIFOStatusTrace(ntaps)
            self.comb += [fifo_status_trace.rd_en.eq(signals['i_fifo_rd_en']),
                fifo_status_trace.empty.eq(fifo_empty)]
            lane_scoreboard = NativeRXLanePopScoreboard(
                ntaps, [lane.dq for lane in layout.lanes])
            self.submodules.rx_lane_scoreboard = lane_scoreboard
            self.comb += [lane_scoreboard.accepted.eq(fifo_status_trace.accepted),
                lane_scoreboard.read_valid.eq(read_valid & ready & ~self._wlevel_en.storage),
                lane_scoreboard.clear.eq(self._debug_clear.wr_stb),
                self._rx_lane_fault.status.eq(lane_scoreboard.fault),
                self._rx_lane_missing.status.eq(lane_scoreboard.missing_lanes),
                self._rx_lane_reads.status.eq(lane_scoreboard.completed_reads)]

        if with_rx_trace:
            from .rx_trace import (NativeReadValidSnapshot, NativeRXTrace,
                logical_dfi_lane_words, logical_dq_words)

            self.submodules.rx_trace = rx_trace = NativeRXTrace(rx_trace_width)
            trace_index = Signal(6)
            self.specials += MultiReg(self._rx_trace_index.storage, trace_index)
            read_valid_snapshot = NativeReadValidSnapshot(
                8*databits + 2*ntaps)
            self.submodules.read_valid_snapshot = read_valid_snapshot
            self.comb += [
                read_valid_snapshot.arm.eq(csr_cdc(
                    self._rx_valid_snapshot_arm.wr_stb)),
                read_valid_snapshot.read_valid.eq(dfi.phases[0].rddata_valid),
                read_valid_snapshot.data.eq(Cat(
                    *[phase.rddata for phase in dfi.phases],
                    fifo_empty, signals['i_fifo_rd_en'])),
                self._rx_valid_snapshot_done.status.eq(
                    read_valid_snapshot.done),
            ]
            for index in range((8*databits + 2*ntaps + 31)//32):
                offset = 32*index
                self.sync += getattr(self, '_rx_valid_snapshot_word'+str(index)).status.eq(
                    read_valid_snapshot.snapshot[offset:min(offset+32,
                        8*databits + 2*ntaps)])
            byte_phy_rden = Cat(*[
                reduce(or_, [signals['i_phy_rden'][4*control]
                    for control in layout.lanes[byte].controls])
                for byte in range(nlanes)])
            dfi_read_command = reduce(or_, [
                ~phase.cs_n & phase.ras_n & ~phase.cas_n & phase.we_n & phase.act_n
                for phase in dfi.phases])
            # The registered serializer inputs are the nearest fabric-visible
            # command point to the native primitives. Each signal has eight
            # DDR slots; preserve CS, CAS, RAS, WE, ACT ordering in the trace.
            launch_cs = self.tx_launch_outputs['i_cs_n_tx_data']
            launch_adr = self.tx_launch_outputs['i_adr_tx_data']
            launch_act = self.tx_launch_outputs['i_act_n_tx_data']
            if (len(launch_cs) != 8 or len(launch_adr) != 17*8 or
                    len(launch_act) != 8):
                raise ValueError('Native RX trace requires eight CA slots')
            trace_trigger = rd.input
            if with_mrs_command_trace:
                mrs_mode = Signal()
                self.specials += MultiReg(self._rx_trace_mrs_mode.storage,
                    mrs_mode)
                mrs_command = reduce(or_, [
                    ~phase.cs_n & ~phase.ras_n & ~phase.cas_n &
                    ~phase.we_n & phase.act_n for phase in dfi.phases])
                trace_trigger = Mux(mrs_mode, mrs_command, rd.input)
                launch_ba = self.tx_launch_outputs['i_ba_tx_data']
                if len(launch_ba) != 16:
                    raise ValueError('MRS trace requires two eight-slot BA lines')
            if rx_trace_lane is None:
                trace_dq_q = signals['o_dq_rx_data']
                trace_dfi_rddata = Cat(*[phase.rddata for phase in dfi.phases])
                trace_dq_bitslip = Cat(*dq_bitslip_outputs)
                trace_dq_bitslip_state = Cat(*dq_bitslip_states)
            else:
                selected_dq = range(rx_trace_lane*8, (rx_trace_lane + 1)*8)
                trace_dq_q = logical_dq_words(signals['o_dq_rx_data'], selected_dq)
                trace_dfi_rddata = logical_dfi_lane_words(
                    dfi.phases, rx_trace_lane, databits=databits)
                trace_dq_bitslip = Cat(*[dq_bitslip_outputs[bit]
                    for bit in selected_dq])
                trace_dq_bitslip_state = Cat(*[dq_bitslip_states[bit]
                    for bit in selected_dq])
            self.comb += [
                rx_trace.arm.eq(csr_cdc(self._rx_trace_arm.wr_stb)),
                rx_trace.trigger.eq(trace_trigger),
                rx_trace.enable.eq(self.read_monitor_enable),
                rx_trace.sample.eq(Cat(trace_dq_q, trace_dfi_rddata, fifo_empty,
                    signals['i_fifo_rd_en'], fifo_status_trace.accepted,
                    fifo_status_trace.q_valid_model,
                    trace_dq_bitslip, trace_dq_bitslip_state,
                    byte_phy_rden, dfi_read_command,
                    # Trace read_valid follows the selected path: fixed
                    # command-relative timing normally, or the scheduled
                    # held-word pulse in software diagnostic mode. It does
                    # not change PhySettings.read_latency/controller timing.
                    rd.input, read_valid, ready, signals['i_phy_rden'],
                    Cat(launch_cs, launch_adr[15*8:16*8],
                        launch_adr[16*8:17*8], launch_adr[14*8:15*8],
                        launch_act),
                    *([Cat(launch_adr[2*8:3*8], launch_ba[0:8],
                        launch_ba[8:16])] if with_mrs_command_trace else []),
                    *([Cat(*monitor.counts)] if with_dqs_wrclk_monitor else []),
                    *([Cat(*dm_monitor.counts)] if with_dm_wrclk_monitor else []),
                    *([self.tx_launch_outputs["i_phy_rden"]]
                        if with_scheduled_fifo_pop else []))),
                rx_trace.index.eq(trace_index),
                self._rx_trace_done.status.eq(rx_trace.done),
            ]
            for index in range(rx_trace_width//32):
                # Register RAM readback before the large CSR mux. Software
                # reads a stopped capture and leaves the index stable.
                self.sync += getattr(self, '_rx_trace_word'+str(index)).status.eq(
                    rx_trace.data[32*index:32*(index+1)])

        if with_rx_boundary_monitor:
            from .rx_trace import (NativeRXBoundaryTrace, NativeRXBoundaryTraceLayout,
                logical_dq_words)

            if len(layout.lanes) < 2:
                raise ValueError('RX boundary monitor requires logical lane 1')
            lane_taps = layout.lanes[1].dq
            selected_bits = range(12, 16)
            selected_taps = [mapping.dq_taps[bit] for bit in selected_bits]
            if any(tap not in lane_taps for tap in selected_taps):
                raise ValueError('RX boundary monitor DQ12..DQ15 are not in logical lane 1')
            control_indices = layout.lanes[1].controls
            if len(control_indices) != 2:
                raise ValueError('RX boundary monitor expects two lane-1 control masks')
            boundary_layout = NativeRXBoundaryTraceLayout()
            self.rx_boundary_trace_layout = dict(
                format_version=2,
                fields=boundary_layout.fields, width=boundary_layout.width,
                depth=32, pretrigger=16, controls=list(control_indices),
                selected_dq=list(selected_bits),
                raw_dq_indexing='logical_dq_bits',
                read_latency=self.settings.read_latency,
                dfi_dq_alias='dq_returned (NativeRXBitslip output wires each DFI phase)',
                mark='first accepted Native DMA repeat-read request',
                trigger='first accepted lane-1 DQ FIFO pop after mark',
                mark_to_pop_timeout_cycles=4096, armed_on_reset=True,
                capture_count=1)
            boundary_trace = NativeRXBoundaryTrace(boundary_layout.payload_width)
            self.submodules.rx_boundary_trace = boundary_trace
            # o_dq_rx_data is adapter-reordered to logical DQ order. Use
            # selected_bits here; selected_taps apply only to physical FIFO
            # state/Q vectors below.
            raw_dq = logical_dq_words(signals['o_dq_rx_data'], selected_bits)
            returned_dq = Cat(*[dq_bitslip_outputs[bit] for bit in selected_bits])
            lane_empty = Cat(*[fifo_empty[tap] for tap in lane_taps])
            lane_rd_en = Cat(*[signals['i_fifo_rd_en'][tap] for tap in lane_taps])
            lane_accepted = Cat(*[fifo_status_trace.accepted[tap] for tap in lane_taps])
            control_rden = Cat(*[signals['i_phy_rden'][4*control:4*control+4]
                for control in control_indices])
            dfi_read_valid = reduce(or_, [phase.rddata_valid for phase in dfi.phases])
            boundary_mark = csr_cdc(self.rx_boundary_trigger)
            # The monitor's repeat mark is a one-cycle sys-to-sys2x event.
            # The ring records it with the sample so software can recover both
            # event cycle numbers from the frozen trace itself.
            boundary_header = csr_status_cdc(Cat(boundary_trace.done,
                boundary_trace.mark_seen, boundary_trace.trigger_seen,
                boundary_trace.trigger_timeout, boundary_trace.start_index,
                boundary_trace.pre_samples, boundary_trace.mark_cycle))
            self.comb += [
                boundary_trace.mark.eq(boundary_mark),
                boundary_trace.lane_pop.eq(reduce(or_, [
                    fifo_status_trace.accepted[tap] for tap in lane_taps])),
                boundary_trace.sample.eq(Cat(rd.input, control_rden,
                    lane_empty, lane_rd_en, lane_accepted, raw_dq,
                    returned_dq, dfi_read_valid, boundary_mark)),
                self._rx_boundary_done.status.eq(boundary_header[0]),
                self._rx_boundary_mark_seen.status.eq(boundary_header[1]),
                self._rx_boundary_trigger_seen.status.eq(boundary_header[2]),
                self._rx_boundary_trigger_timeout.status.eq(boundary_header[3]),
                self._rx_boundary_start_index.status.eq(boundary_header[4:9]),
                self._rx_boundary_pre_samples.status.eq(boundary_header[9:14]),
                self._rx_boundary_mark_cycle.status.eq(boundary_header[14:30]),
                self._rx_boundary_width.status.eq(boundary_layout.width),
            ]
            sample_index_fast = Signal(5)
            word_index_fast = Signal(3)
            read_ack_fast = Signal()
            read_request_fast = csr_cdc(self._rx_boundary_read.wr_stb)
            self.sync += If(read_request_fast,
                sample_index_fast.eq(self._rx_boundary_read_sample.storage),
                word_index_fast.eq(self._rx_boundary_read_word.storage),
                read_ack_fast.eq(~read_ack_fast))
            self.comb += boundary_trace.sample_index.eq(sample_index_fast)
            word_values = []
            for word in range((boundary_layout.width + 31)//32):
                start = 32*word
                part = boundary_trace.data[start:min(start+32, boundary_layout.width)]
                if len(part) < 32:
                    part = Cat(part, Constant(0, 32-len(part)))
                word_values.append(part)
            selected_word = Array(word_values)[word_index_fast]
            self.comb += [
                self._rx_boundary_read_ack.status.eq(csr_status_cdc(read_ack_fast)),
                self._rx_boundary_word.status.eq(csr_status_cdc(selected_word)),
            ]

        # Capture fabric-side native data/status only: no additional loads on
        # dedicated serial DATAIN or T_OUT routes. First sample follows trigger.
        if with_debug:
            trace      = Memory(32*trace_words, 64)
            write_port = trace.get_port(write_capable=True)
            read_port  = trace.get_port()
            self.specials += trace, write_port, read_port
            trigger = rd.input | pulse['wlevel_strobe']
            trace_sample = Cat(signals['o_dq_rx_data'], fifo_empty,
                ready, rd.input, pulse['wlevel_strobe'], self._wlevel_en.storage,
                data_tristate, dqs_drive, signals['i_phy_rden'], selected_pattern,
                dqs_data[:8])
            self.submodules.debug_trace_capture = debug_trace_capture = \
                NativeDebugTraceCapture(len(trace_sample))
            trace_index = Signal(6)
            trace_arm = csr_cdc(self._trace_arm.wr_stb)
            self.comb += [debug_trace_capture.arm.eq(trace_arm),
                debug_trace_capture.trigger.eq(trigger),
                debug_trace_capture.sample.eq(trace_sample),
                write_port.adr.eq(debug_trace_capture.write_address),
                write_port.we.eq(debug_trace_capture.write_enable),
                write_port.dat_w.eq(debug_trace_capture.write_data),
                read_port.adr.eq(trace_index)]
            self.specials += MultiReg(self._trace_index.storage, trace_index)
            self.comb += [
                self._trace_state.status.eq(Cat(debug_trace_capture.pending,
                    debug_trace_capture.running)),
                self._trace_done.status.eq(debug_trace_capture.done),
                read_port.adr.eq(trace_index)]
            # The CSR bus runs in the system domain. Synchronize its write
            # pulse before it controls state in the native trace domain.
            for word in range(trace_words):
                # The stopped capture is read after the index settles. Move
                # its BRAM output into the CSR clock before the CSR read mux;
                # the direct fast-BRAM-to-slow-CSR path limits debug builds.
                self.comb += getattr(self, '_trace_word'+str(word)).status.eq(
                    csr_status_cdc(read_port.dat_r[32*word:32*word+32]))
