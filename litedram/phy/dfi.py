#
# This file is part of LiteDRAM.
#
# Copyright (c) 2015 Sebastien Bourdeauducq <sb@m-labs.hk>
# Copyright (c) 2021 Antmicro <www.antmicro.com>
# SPDX-License-Identifier: BSD-2-Clause

import inspect

from migen import *
from migen.genlib.record import *
from migen.genlib.cdc import PulseSynchronizer

from litedram.common import PhySettings
from litedram.phy.utils import Serializer, Deserializer


def phase_cmd_description(addressbits, bankbits, nranks):
    return [
        ("address", addressbits, DIR_M_TO_S),
        ("bank",       bankbits, DIR_M_TO_S),
        ("cas_n",             1, DIR_M_TO_S),
        ("cs_n",         nranks, DIR_M_TO_S),
        ("ras_n",             1, DIR_M_TO_S),
        ("we_n",              1, DIR_M_TO_S),
        ("cke",          nranks, DIR_M_TO_S),
        ("odt",          nranks, DIR_M_TO_S),
        ("reset_n",           1, DIR_M_TO_S),
        ("act_n",             1, DIR_M_TO_S)
    ]


def phase_wrdata_description(databits):
    return [
        ("wrdata",         databits, DIR_M_TO_S),
        ("wrdata_en",             1, DIR_M_TO_S),
        ("wrdata_mask", databits//8, DIR_M_TO_S)
    ]


def phase_rddata_description(databits):
    return [
        ("rddata_en",           1, DIR_M_TO_S),
        ("rddata",       databits, DIR_S_TO_M),
        ("rddata_valid",        1, DIR_S_TO_M)
    ]


def phase_description(addressbits, bankbits, nranks, databits):
    r = phase_cmd_description(addressbits, bankbits, nranks)
    r += phase_wrdata_description(databits)
    r += phase_rddata_description(databits)
    return r


class Interface(Record):
    def __init__(self, addressbits, bankbits, nranks, databits, nphases=1):
        layout = [("p"+str(i), phase_description(addressbits, bankbits, nranks, databits)) for i in range(nphases)]
        Record.__init__(self, layout)
        self.phases = [getattr(self, "p"+str(i)) for i in range(nphases)]
        for p in self.phases:
            p.cas_n.reset = 1
            p.cs_n.reset = (2**nranks-1)
            p.ras_n.reset = 1
            p.we_n.reset = 1
            p.act_n.reset = 1

    # Returns pairs (DFI-mandated signal name, Migen signal object)
    def get_standard_names(self, m2s=True, s2m=True):
        r = []
        add_suffix = len(self.phases) > 1
        for n, phase in enumerate(self.phases):
            for field, size, direction in phase.layout:
                if (m2s and direction == DIR_M_TO_S) or (s2m and direction == DIR_S_TO_M):
                    if add_suffix:
                        if direction == DIR_M_TO_S:
                            suffix = "_p" + str(n)
                        else:
                            suffix = "_w" + str(n)
                    else:
                        suffix = ""
                    r.append(("dfi_" + field + suffix, getattr(phase, field)))
        return r


class Interconnect(Module):
    def __init__(self, master, slave):
        self.comb += master.connect(slave)


class DDR4DFIMux(Module):
    def __init__(self, dfi_i, dfi_o):
        for i in range(len(dfi_i.phases)):
            p_i = dfi_i.phases[i]
            p_o = dfi_o.phases[i]
            self.comb += [
                p_i.connect(p_o),
                If(~p_i.ras_n & p_i.cas_n & p_i.we_n,
                   p_o.act_n.eq(0),
                   p_o.we_n.eq(p_i.address[14]),
                   p_o.cas_n.eq(p_i.address[15]),
                   p_o.ras_n.eq(p_i.address[16])
                ).Else(
                    p_o.act_n.eq(1),
                )
            ]


class DDR3DFIMux(Module):
    """Map DDR3 DFI phases to the direct DDR3 command/address convention.

    DDR3 carries RAS_n, CAS_n and WE_n on dedicated pins and has no ACT_n
    pin. Its command/address fields therefore pass through unchanged; the
    generic DFI ACT_n field is held inactive. This block only performs that
    protocol-level mapping. It does not implement a PHY, serialization,
    training or calibration.
    """
    def __init__(self, dfi_i, dfi_o):
        if len(dfi_i.phases) != len(dfi_o.phases):
            raise ValueError("DDR3 DFI interfaces must have the same phase count")
        for p_i, p_o in zip(dfi_i.phases, dfi_o.phases):
            if (len(p_i.address) != len(p_o.address) or
                    len(p_i.bank) != len(p_o.bank) or
                    len(p_i.cs_n) != len(p_o.cs_n)):
                raise ValueError("DDR3 DFI interfaces must have matching command widths")
            if (len(p_i.wrdata) != len(p_o.wrdata) or
                    len(p_i.wrdata_mask) != len(p_o.wrdata_mask) or
                    len(p_i.rddata) != len(p_o.rddata)):
                raise ValueError("DDR3 DFI interfaces must have matching data widths")
            self.comb += [
                p_i.connect(p_o),
                p_o.act_n.eq(1),
            ]


class DFIRateConverter(Module):
    """Converts between DFI interfaces running at different clock frequencies

    This module allows to convert DFI interface `phy_dfi` running at higher clock frequency
    into a DFI interface running at `ratio` lower frequency. The new DFI has `ratio` more
    phases and the commands on the following phases of the new DFI will be serialized to
    following phases/clocks of `phy_dfi` (phases first, then clock cycles).

    Data must be serialized/deserialized in such a way that a whole burst on `phy_dfi` is
    sent in a single `clk` cycle. For this reason, the new DFI interface will have `ratio`
    less databits by default. For example, with phy_dfi(nphases=2, databits=32) and ratio=4 the
    new DFI will have nphases=8, databits=8. This results in 8*8=64 bits in `clkdiv` translating
    into 2*32=64 bits in `clk`. This means that only a single cycle of `clk` per `clkdiv`
    cycle carries the data (by default cycle 0). `preserve_throughput=True` keeps the PHY data
    width on every new phase instead; it is useful when the lower-frequency DFI must carry all
    `ratio` high-frequency PHY cycles in one controller cycle. In that mode, `write_delay` and
    `read_delay` delay the serialized data stream by fast-clock edges while keeping all data
    phases. The default mode retains its original slice-selection behavior.

    `serdes_reset` optionally holds all fast slot counters at a common phase.
    With phase-aligned clocks, a reset from `clkdiv` can establish a repeatable
    slot boundary after the fast clock domain has left reset. Apply it to both
    directions: resetting only serializers can move reads into an unselected
    deserializer slot. None preserves the original counter reset behavior.

    `align_read_slots` is an experimental extra register on the final
    deserialized chunk. It was used with offset-phase simulation clocks;
    leave it disabled for aligned rising edges, where the normal deserializer
    already returns both slots together. Enabling it there makes slot 1 stale.
    It requires full-rate 2:1 conversion, shared reset and `read_delay=0`.
    """
    def __init__(self, phy_dfi, *, clkdiv, clk, ratio, serdes_reset_cnt=-1, write_delay=0, read_delay=0,
                 preserve_throughput=False, repeat_write_data=False, early_write_data=False,
                 serdes_reset=None, align_read_slots=False, serializer=None, deserializer=None):
        if align_read_slots and (not preserve_throughput or ratio != 2 or read_delay != 0
                or serdes_reset is None):
            raise ValueError("Aligned read slots require full-rate 2:1 conversion, even read latency and shared reset")
        if not preserve_throughput:
            assert len(phy_dfi.p0.wrdata) % ratio == 0
        assert 0 <= write_delay < ratio, f"Data can be delayed up to {ratio} clk cycles"
        assert 0 <= read_delay < ratio, f"Data can be delayed up to {ratio} clk cycles"
        if preserve_throughput:
            assert ratio > 1, "Throughput-preserving conversion requires ratio > 1"
        if repeat_write_data:
            assert not preserve_throughput, "Repeated write data applies to reduced-width conversion"
        if early_write_data:
            assert not preserve_throughput, "Early write data applies to reduced-width conversion"

        # Serializer/Deserializer classes (same interface as the default ones, e.g. a PHY specific
        # clock domain crossing).
        serializer   = serializer   or Serializer
        deserializer = deserializer or Deserializer
        self.ser_latency = serializer.LATENCY
        self.des_latency = deserializer.LATENCY

        phase_params = dict(
            addressbits = len(phy_dfi.p0.address),
            bankbits = len(phy_dfi.p0.bank),
            nranks = len(phy_dfi.p0.cs_n),
            databits = len(phy_dfi.p0.wrdata) if preserve_throughput else len(phy_dfi.p0.wrdata) // ratio,
        )
        self.dfi = Interface(nphases=ratio * len(phy_dfi.phases), **phase_params)

        def delay_fast(signal, cycles):
            """Delay a complete signal stream by `cycles` fast-clock edges."""
            for _ in range(cycles):
                delayed = Signal.like(signal)
                getattr(self.sync, clk).__iadd__(delayed.eq(signal))
                signal = delayed
            return signal

        wr_delayed = ["wrdata", "wrdata_mask"]
        rd_delayed = ["rddata", "rddata_valid"]

        for name, width, dir in phase_description(**phase_params):
            # all signals except write/read
            if name in wr_delayed + rd_delayed:
                continue
            # on each clk phase
            for pi, phase_s in enumerate(phy_dfi.phases):
                sig_s = getattr(phase_s, name)
                assert len(sig_s) == width

                # data from each clkdiv phase
                sigs_m = []
                for j in range(ratio):
                    phase_m = self.dfi.phases[pi + len(phy_dfi.phases)*j]
                    sigs_m.append(getattr(phase_m, name))

                ser = serializer(
                    clkdiv     = clkdiv,
                    clk       = clk,
                    i_dw      = ratio*width,
                    o_dw      = width,
                    i         = Cat(sigs_m),
                    o         = sig_s,
                    reset     = serdes_reset,
                    reset_cnt = serdes_reset_cnt,
                    name      = name,
                )
                self.submodules += ser

        # wrdata
        for name, width, dir in phase_description(**phase_params):
            if name not in wr_delayed:
                continue
            for pi, phase_s in enumerate(phy_dfi.phases):
                sig_s = getattr(phase_s, name)
                sig_m = Signal(len(sig_s) * ratio)

                sigs_m = []
                for j in range(ratio):
                    phase_index = pi + len(phy_dfi.phases)*j if preserve_throughput else pi*ratio + j
                    phase_m = self.dfi.phases[phase_index]
                    sigs_m.append(getattr(phase_m, name))

                width = len(Cat(sigs_m))
                if preserve_throughput:
                    self.comb += sig_m.eq(Cat(sigs_m))
                elif repeat_write_data:
                    # A reduced-width BL8 occupies one fast transaction. Hold its
                    # payload and byte mask over every fast slot so a one-slot
                    # startup phase difference cannot leave the DQS burst with
                    # the converter's zero-filled inactive slot. Commands and
                    # wrdata_en are still emitted only in their selected slot.
                    self.comb += sig_m.eq(Replicate(Cat(sigs_m), ratio))
                else:
                    self.comb += sig_m[write_delay*width:(write_delay+1)*width].eq(Cat(sigs_m))

                o = Signal.like(sig_s)
                ser = serializer(
                    clkdiv     = clkdiv,
                    clk       = clk,
                    i_dw      = len(sig_m),
                    o_dw      = len(sig_s),
                    i         = sig_m,
                    o         = o,
                    reset     = serdes_reset,
                    reset_cnt = serdes_reset_cnt,
                    register  = not early_write_data,
                    name      = name,
                )
                self.submodules += ser

                if preserve_throughput:
                    o = delay_fast(o, write_delay)
                self.comb += sig_s.eq(o)

        # rddata
        for name, width, dir in phase_description(**phase_params):
            if name not in rd_delayed:
                continue
            for pi, phase_s in enumerate(phy_dfi.phases):
                sig_s = getattr(phase_s, name)

                sig_m = Signal(ratio * len(sig_s))
                sigs_m = []
                for j in range(ratio):
                    phase_index = pi + len(phy_dfi.phases)*j if preserve_throughput else pi*ratio + j
                    phase_m = self.dfi.phases[phase_index]
                    sigs_m.append(getattr(phase_m, name))

                if preserve_throughput:
                    sig_s = delay_fast(sig_s, read_delay)

                des = deserializer(
                    clkdiv    = clkdiv,
                    clk       = clk,
                    i_dw      = len(sig_s),
                    o_dw      = len(sig_m),
                    i         = sig_s,
                    o         = sig_m,
                    reset     = serdes_reset,
                    reset_cnt = serdes_reset_cnt,
                    name      = name,
                )
                self.submodules += des

                if preserve_throughput:
                    if align_read_slots:
                        # Experimental offset-phase compensation. Aligned
                        # rising-edge clocks must use the unmodified return
                        # path below; this extra register makes slot 1 stale.
                        self.comb += sigs_m[0].eq(sig_m[:len(sig_s)])
                        read_sync = getattr(self.sync, clkdiv)
                        read_sync += sigs_m[1].eq(sig_m[len(sig_s):])
                    else:
                        self.comb += Cat(sigs_m).eq(sig_m)
                elif name == "rddata_valid":
                    self.comb += Cat(sigs_m).eq(Replicate(sig_m[read_delay], ratio))
                else:
                    out_width = len(Cat(sigs_m))
                    sig_m_window = sig_m[read_delay*out_width:(read_delay + 1)*out_width]
                    self.comb += Cat(sigs_m).eq(sig_m_window)

    @classmethod
    def phy_wrapper(cls, phy_cls, ratio, phy_attrs=None, clock_mapping=None, **converter_kwargs):
        """Generate a wrapper class for given PHY

        Given PHY `phy_cls` a new Module is generated, which will instantiate `phy_cls` as a
        submodule (self.submodules.phy), with DFIRateConverter used to convert its DFI. It will
        recalculate `phy_cls` PhySettings to have correct latency values.

        Parameters
        ----------
        phy_cls : type
            PHY class. It must support a `csr_cdc` argument (function: csr_cdc(Signal) -> Signal)
            that it will use to wrap all CSR.wr_stb signals to avoid clock domain crossing problems.
        ratio : int
            Frequency ratio between the new DFI and the DFI of the wrapped PHY.
        phy_attrs : list[str]
            Names of PHY attributes to be copied to the wrapper (self.attr = self.phy.attr).
        clock_mapping : dict[str, str]
            Clock remapping for the PHY. Defaults to {"sys": f"sys{ratio}x"}.
        converter_kwargs : Any
            Keyword arguments forwarded to the DFIRateConverter instance.
        """
        if ratio == 1:
            return phy_cls

        # Generate the wrapper class dynamically
        name = f"{phy_cls.__name__}Wrapper"
        bases = (Module, object, )

        internal_cd = f"sys{ratio}x"
        if clock_mapping is None:
            clock_mapping = {"sys": internal_cd}

        # Constructor
        def __init__(self, *args, **kwargs):
            # Add the PHY in new clock domain,
            self.internal_cd = internal_cd
            phy_kwargs = dict(kwargs, csr_cdc=self.csr_cdc)
            if 'csr_status_cdc' in inspect.signature(phy_cls.__init__).parameters:
                phy_kwargs['csr_status_cdc'] = self.csr_status_cdc
            phy = phy_cls(*args, **phy_kwargs)

            # Remap clock domains in the PHY
            # Workaround: do this in two steps to avoid errors due to the fact that renaming is done
            # sequentially. Consider mapping {"sys": "sys2x", "sys2x": "sys4x"}, it would lead to:
            #   sys2x = sys
            #   sys4x = sys2x
            # resulting in all sync operations in sys4x domain.
            mapping = [tuple(i) for i in clock_mapping.items()]
            map_tmp = {clk_from: f"tmp{i}" for i, (clk_from, clk_to) in enumerate(mapping)}
            map_final = {f"tmp{i}": clk_to for i, (clk_from, clk_to) in enumerate(mapping)}
            self.submodules.phy = ClockDomainsRenamer(map_final)(ClockDomainsRenamer(map_tmp)(phy))

            # Copy some attributes of the PHY
            for attr in phy_attrs or []:
                setattr(self, attr, getattr(self.phy, attr))

            # Insert DFI rate converter to
            self.submodules.dfi_converter = DFIRateConverter(phy.dfi,
                clkdiv      = "sys",
                clk         = self.internal_cd,
                ratio       = ratio,
                write_delay = phy.settings.write_latency % ratio,
                read_delay  = phy.settings.read_latency % ratio,
                **converter_kwargs,
            )
            self.dfi = self.dfi_converter.dfi

            # Generate new PhySettings
            converter_latency = self.dfi_converter.ser_latency + self.dfi_converter.des_latency
            read_latency = phy.settings.read_latency
            if converter_kwargs.get("preserve_throughput", False):
                # Full-width conversion delays the complete fast return
                # stream before packing slots. Account for that delay when
                # predicting the slow fixed-latency owner tag. Reduced-width
                # conversion selects a window instead and retains its timing.
                read_latency += phy.settings.read_latency % ratio
            self.settings = PhySettings(
                phytype                   = phy.settings.phytype,
                memtype                   = phy.settings.memtype,
                databits                  = phy.settings.databits,
                dfi_databits              = len(self.dfi.p0.wrdata),
                nranks                    = phy.settings.nranks,
                nphases                   = len(self.dfi.phases),
                rdphase                   = phy.settings.rdphase,
                wrphase                   = phy.settings.wrphase,
                cl                        = phy.settings.cl,
                cwl                       = phy.settings.cwl,
                read_latency              = read_latency//ratio + converter_latency,
                write_latency             = phy.settings.write_latency//ratio,
                cmd_latency               = phy.settings.cmd_latency,
                cmd_delay                 = phy.settings.cmd_delay,
                write_leveling            = phy.settings.write_leveling,
                write_dq_dqs_training     = phy.settings.write_dq_dqs_training,
                write_latency_calibration = phy.settings.write_latency_calibration,
                read_leveling             = phy.settings.read_leveling,
                delays                    = phy.settings.delays,
                bitslips                  = phy.settings.bitslips,
            )

            # Copy any non-default PhySettings (e.g. electrical settings)
            for attr, value in vars(self.phy.settings).items():
                if not hasattr(self.settings, attr):
                    setattr(self.settings, attr, value)

        def csr_cdc(self, i):
            o = Signal()
            psync = PulseSynchronizer("sys", self.internal_cd)
            self.submodules += psync
            self.comb += [
                psync.i.eq(i),
                o.eq(psync.o),
            ]
            return o

        def csr_status_cdc(self, i, invalidation=None, source_invalidation=None):
            # The PHY owns the fast-domain status source. Return it through a
            # registered bridge so CSR readback stays in the outer sys domain.
            from litedram.phy.usnative.tap_status import CSRStatusBridge
            bridge = CSRStatusBridge(len(i), self.internal_cd, "sys")
            self.submodules += bridge
            self.comb += bridge.source.eq(i)
            if invalidation is not None:
                self.comb += bridge.invalidate.eq(invalidation)
            if source_invalidation is not None:
                self.comb += bridge.source_invalidate.eq(source_invalidation)
            return bridge.status

        def get_csrs(self):
            return self.phy.get_csrs()

        # This creates a new class with given name, base classes and attributes/methods
        namespace = dict(
            __init__ = __init__,
            csr_cdc  = csr_cdc,
            csr_status_cdc = csr_status_cdc,
            get_csrs = get_csrs,
        )
        return type(name, bases, namespace)
