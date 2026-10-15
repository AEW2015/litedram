"""Exact decrement/reload timer with registered lower-prefix borrow enables."""
from migen import *
from migen.fhdl.bitcontainer import bits_for
class SegmentedRefreshTimer(Module):
    def __init__(self, cycles):
        assert cycles>=1
        self.wait=Signal();self.done=Signal()
        width=bits_for(cycles);reload=cycles-1
        self.count=Signal(width,reset=reload)
        self.prefix_zero={}
        decrement=self.wait & ~self.done
        for lo in range(0,width,6):
            hi=min(lo+6,width)
            if lo:
                zero=Signal(reset=int((reload & ((1<<lo)-1))==0),name=f'prefix_zero_{lo}')
                zero.attr.add('preserve');zero.attr.add('no_retiming')
                self.prefix_zero[lo]=zero
                # After a decrement the next low prefix is zero exactly when
                # its previous unsigned value was1. Otherwise reload/reset.
                self.sync += If(decrement,zero.eq(self.count[:lo]==1)).Else(zero.eq(zero.reset))
                borrow=zero
            else:borrow=1
            self.sync += If(decrement,self.count[lo:hi].eq(self.count[lo:hi]-borrow)).Else(self.count[lo:hi].eq((reload>>lo)&((1<<(hi-lo))-1)))
        # The cached current lower prefix is already exact. Reuse it for
        # expiration instead of broadcasting all29 count bits into a wide OR.
        # This changes no timer cycle, reload, count or downstream PHY schedule.
        if self.prefix_zero:
            prefix_lo=max(self.prefix_zero)
            self.comb += self.done.eq(self.prefix_zero[prefix_lo] & (self.count[prefix_lo:]==0))
        else:
            self.comb += self.done.eq(self.count==0)
