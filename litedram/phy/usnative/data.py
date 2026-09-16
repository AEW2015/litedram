# SPDX-License-Identifier: BSD-2-Clause
"""Four-phase DFI data packing for eight-bit native serializers.

Bitslip and the final launch register belong after this packing. RX inputs
must already have undergone lane alignment and burst-local RX bitslip.
"""
from migen import Cat, Module, Signal


class NativeDataPacking(Module):
    def __init__(self, dfi, *, databits):
        if type(databits) is not int or databits <= 0 or databits % 8:
            raise ValueError('Native data width must be a positive multiple of eight')
        if len(dfi.phases) != 4:
            raise ValueError('Native data packing requires four DFI phases')
        masks = databits // 8
        for phase in dfi.phases:
            if (len(phase.wrdata) != 2 * databits or len(phase.rddata) != 2 * databits
                    or len(phase.wrdata_mask) != 2 * masks):
                raise ValueError('DFI width does not match the native data width')
        self.tx_dq = [Signal(8) for _ in range(databits)]
        self.tx_dm_n = [Signal(8) for _ in range(masks)]
        self.rx_dq = [Signal(8) for _ in range(databits)]
        for bit in range(databits):
            self.comb += self.tx_dq[bit].eq(Cat(*[
                dfi.phases[edge // 2].wrdata[(edge % 2) * databits + bit]
                for edge in range(8)]))
            for edge in range(8):
                self.comb += dfi.phases[edge // 2].rddata[
                    (edge % 2) * databits + bit].eq(self.rx_dq[bit][edge])
        for bit in range(masks):
            self.comb += self.tx_dm_n[bit].eq(Cat(*[
                ~dfi.phases[edge // 2].wrdata_mask[(edge % 2) * masks + bit]
                for edge in range(8)]))
