"""Two-entry elastic stream buffer with a stable registered head.

The interface and cycle behavior match V1. The active output comes directly
from the head registers; no read pointer or payload read mux is on that path.
"""

from migen import If, Module, Signal, Mux
from litex.soc.interconnect import stream


class FullRateElasticBuffer(Module):
    def __init__(self, layout):
        self.sink   = sink   = stream.Endpoint(layout)
        self.source = source = stream.Endpoint(layout)

        nonempty = Signal(reset=0)
        full = Signal(reset=0)
        nonempty.attr.add("no_retiming")
        full.attr.add("no_retiming")
        self.nonempty = nonempty
        self.full = full

        slots = [stream.Endpoint(layout), stream.Endpoint(layout)]
        self.slots = slots
        head, tail = slots
        for slot in slots:
            slot.first.reset_less = True
            slot.last.reset_less = True
            for field, _ in slot.payload.iter_flat():
                field.reset_less = True
            for field, _ in slot.param.iter_flat():
                field.reset_less = True

        push = sink.valid & sink.ready
        pop = source.valid & source.ready
        self.comb += [
            sink.ready.eq(~full),
            source.valid.eq(nonempty),
            source.first.eq(head.first),
            source.last.eq(head.last),
        ]
        for (out, _), (head_field, _) in zip(source.payload.iter_flat(), head.payload.iter_flat()):
            self.comb += out.eq(head_field)
        for (out, _), (head_field, _) in zip(source.param.iter_flat(), head.param.iter_flat()):
            self.comb += out.eq(head_field)

        # Payload is don't-care while its slot is invalid. Refresh head
        # whenever empty or consumed, and tail whenever it is not full.
        # These enables avoid sink.valid and the push/pop priority chain.
        head_write = ~nonempty | source.ready
        tail_write = ~full
        head_assignments = [head.first.eq(sink.first), head.last.eq(sink.last)]
        head_assignments += [dst.eq(src) for (dst, _), (src, _) in zip(
            head.payload.iter_flat(), sink.payload.iter_flat())]
        head_assignments += [dst.eq(src) for (dst, _), (src, _) in zip(
            head.param.iter_flat(), sink.param.iter_flat())]
        tail_assignments = [tail.first.eq(sink.first), tail.last.eq(sink.last)]
        tail_assignments += [dst.eq(src) for (dst, _), (src, _) in zip(
            tail.payload.iter_flat(), sink.payload.iter_flat())]
        tail_assignments += [dst.eq(src) for (dst, _), (src, _) in zip(
            tail.param.iter_flat(), sink.param.iter_flat())]
        head_assignments = [head.first.eq(Mux(full, tail.first, sink.first)),
            head.last.eq(Mux(full, tail.last, sink.last))]
        head_assignments += [dst.eq(Mux(full, old, new))
            for (dst, _), (old, _), (new, _) in zip(head.payload.iter_flat(),
                tail.payload.iter_flat(), sink.payload.iter_flat())]
        head_assignments += [dst.eq(Mux(full, old, new))
            for (dst, _), (old, _), (new, _) in zip(head.param.iter_flat(),
                tail.param.iter_flat(), sink.param.iter_flat())]

        self.sync += [
            If(head_write, *head_assignments),
            If(tail_write, *tail_assignments),
            full.eq(~pop & (full | (push & nonempty))),
            nonempty.eq(push | (nonempty & ~(pop & ~full))),
        ]
        self.level = Signal(2)
        self.comb += self.level.eq(nonempty + full)


ElasticBuffer = FullRateElasticBuffer
