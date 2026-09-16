# SPDX-License-Identifier: BSD-2-Clause
from migen import *

class RegisteredTapStatus(Module):
    """Registered status tree with explicit settling invalidation.

    change must include every selection and delay-control update. 32 sys cycles
    cover the 2:1 destination clock, pulse CDC and pipeline. Native analog delay
    update settling remains subject to hardware qualification.
    """
    def __init__(self,entries):
        if not isinstance(entries, int) or entries < 1:
            raise ValueError('At least one tap-status entry is required')
        self.source=Signal(9*entries);self.select=Signal(max=max(2, entries))
        self.change=Signal();self.ready=Signal();self.value=Signal(9);self.valid=Signal()
        local=Signal(len(self.source));sampled=Signal(len(self.source));age=Signal(6)
        self.sync.riu += local.eq(self.source)
        self.sync += sampled.eq(local)
        groups=[]
        for start in range(0,entries,8):
            group=Signal(9);group.attr.add('dont_touch')
            values=[sampled[9*i:9*i+9] for i in range(start,min(start+8,entries))]
            values += [Constant(0,9)]*(8-len(values))
            self.sync += group.eq(Array(values)[self.select[:3]])
            groups.append(group)
        group_index = self.select[3:] if len(self.select) > 3 else Constant(0)
        self.sync += [self.value.eq(Mux(self.select<entries,Array(groups)[group_index],0)),
            If(~self.ready | self.change,age.eq(0)).Elif(age<32,age.eq(age+1))]
        self.comb += self.valid.eq((age==32) & self.ready & ~self.change)
