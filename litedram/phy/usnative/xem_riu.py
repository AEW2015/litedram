#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""XEM8320 existing native-core RIU ABI: eight nibbles, four byte strobes."""
from migen import Module, Signal


class XEMRIUWriteEnable(Module):
    def __init__(self):
        self.select = Signal(8)
        self.write = Signal()
        self.enable = Signal(4)
        for byte in range(4):
            self.comb += self.enable[byte].eq(
                self.write & (self.select[2*byte] | self.select[2*byte+1]))
