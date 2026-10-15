"""Balanced one-hot read-data mux for a captured CSR address."""
from migen import *


class FullRateCSRReadMux(Module):
    """Select one of the mapped 32-bit CSR words from a registered one-hot decode.

    Unmapped addresses produce zero, matching the default branch of a Case mux.
    """
    def __init__(self, address_select, values):
        if len(address_select) < max(values, default=-1) + 1:
            raise ValueError("address_select does not cover all mapped CSR words")
        self.data = Signal(32)
        terms = [Mux(address_select[index], value, Constant(0, 32))
                 for index, value in values.items()]
        self.comb += self.data.eq(reduce(or_, terms) if terms else Constant(0, 32))
