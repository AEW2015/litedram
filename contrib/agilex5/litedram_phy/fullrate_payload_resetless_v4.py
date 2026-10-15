"""V2 payload tagging plus valid-guarded fully registered elastic slots."""
from litedram_phy.fullrate_payload_resetless_v2 import apply_payload_reset_less_v2, _payload_signals
from litedram_phy.fullrate_elastic_buffer_v3 import FullRateElasticBuffer


def apply_payload_reset_less_v4(root):
    count = apply_payload_reset_less_v2(root)
    seen = set()
    signals = set()
    def walk(module):
        if id(module) in seen:
            return
        seen.add(id(module))
        if isinstance(module,FullRateElasticBuffer):
            for slot in module.slots:
                for signal in _payload_signals(slot.payload):
                    assert signal.reset_less
                    signals.add(id(signal))
        for _,child in getattr(module,'_submodules',[]):
            walk(child)
    walk(root)
    return count + len(signals)
