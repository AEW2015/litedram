"""Generic reset-less payload-flop tagging for LiteX stream pipelines.

Call after DUT elaboration and before conversion. Only data payload registers
owned by PipeValid and PipeReady are changed; valid/control/param registers and
FIFO occupancy, pointers, and reset behavior are untouched.
"""
from migen import Signal
from migen.fhdl.structure import _Assign, If
from litex.soc.interconnect import stream


def _payload_signals(payload):
    if isinstance(payload, Signal):
        return [payload]
    return [signal for signal, _ in payload.iter_flat()]


def _walk_statements(statements):
    for statement in statements:
        if isinstance(statement, _Assign):
            yield statement
        elif isinstance(statement, If):
            yield from _walk_statements(statement.t)
            yield from _walk_statements(statement.f)
        elif isinstance(statement, (list, tuple)):
            yield from _walk_statements(statement)


def _tag_pipe_valid(module):
    payload = _payload_signals(module.source.payload)
    for signal in payload:
        signal.reset_less = True
    return payload


def _tag_pipe_ready(module):
    # PipeReady captures a hidden Endpoint named sink_d in its stall skid.
    # Recover only payload LHS registers by matching assignments from sink's
    # payload signals; this excludes its valid/first/last/param control fields.
    sources = set(_payload_signals(module.sink.payload))
    matched = {}
    for statements in module._fragment.sync.values():
        for assignment in _walk_statements(statements):
            if assignment.r in sources and isinstance(assignment.l, Signal):
                assignment.l.reset_less = True
                matched[assignment.r] = assignment.l
    if set(matched) != sources:
        raise ValueError("could not identify all PipeReady sink_d payload registers")
    return list(matched.values())


def apply_payload_reset_less_v2(root):
    """Tag stream payload pipeline flops throughout a Migen/LiteX module tree.

    Covers OutputRegisteredFIFO internals and standalone stream.Buffer instances
    such as command-input, DMA write-output, and return buffers. Returns the
    number of unique payload Signal registers changed.
    """
    seen_modules = set()
    changed_signals = {}

    def walk(module):
        if id(module) in seen_modules:
            return
        seen_modules.add(id(module))
        if isinstance(module, stream.PipeValid):
            for signal in _tag_pipe_valid(module):
                changed_signals[id(signal)] = signal
        elif isinstance(module, stream.PipeReady):
            for signal in _tag_pipe_ready(module):
                changed_signals[id(signal)] = signal
        for _, submodule in getattr(module, "_submodules", []):
            walk(submodule)

    walk(root)
    return len(changed_signals)
