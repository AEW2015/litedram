"""Isolated construction-time substitution of full elastic stream buffers."""
from contextlib import contextmanager
from litex.soc.interconnect import stream
from litedram_phy.fullrate_elastic_buffer_v3 import FullRateElasticBuffer


@contextmanager
def registered_elastic_buffers():
    original = stream.Buffer
    def make_buffer(layout, pipe_valid=True, pipe_ready=False):
        if pipe_valid and pipe_ready:
            return FullRateElasticBuffer(layout)
        return original(layout, pipe_valid=pipe_valid, pipe_ready=pipe_ready)
    stream.Buffer = make_buffer
    try:
        yield
    finally:
        stream.Buffer = original
