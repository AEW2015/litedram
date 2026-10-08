"""CK-level prototype for 150 MHz dual-BL8 DDR4 command scheduling.

This scheduler models ready CAS requests after bank management has made them
eligible. It checks command spacing and pairs each command with its own payload.
"""
from dataclasses import dataclass
import unittest

CK_PER_SLOW_CYCLE = 8
TCCD_S_CK = 4
TCCD_L_CK = 8
BURST_BYTES = 32


@dataclass(frozen=True)
class Request:
    op: str
    group: int
    bank: int
    row: int
    col: int
    payload: bytes = b""
    mask: int = 0
    tag: int = 0

    def __post_init__(self):
        if self.op not in ("read", "write"):
            raise ValueError("op must be read or write")
        if self.group not in (0, 1) or self.bank not in range(4):
            raise ValueError("DDR4 group/bank out of range")
        if self.op == "write" and len(self.payload) != BURST_BYTES:
            raise ValueError("each BL8 write slot must carry 32 bytes")
        if self.op == "read" and self.payload:
            raise ValueError("read request cannot carry write payload")
        if self.mask < 0 or self.mask >= (1 << BURST_BYTES):
            raise ValueError("mask must contain one bit per byte")


@dataclass(frozen=True)
class Slot:
    ck: int
    request: Request


class DualSlotScheduler:
    """Greedy two-slot-per-cycle model with DDR4 tCCD legality."""
    def __init__(self, *, write_to_read_ck=0, read_to_write_ck=0):
        self.last_cas_group = None
        self.last_cas_ck = None
        self.last_op = None
        self.last_op_ck = None
        self.cycle_index = 0
        # These are caller-supplied CK floors for a controller/PHY model.
        # Real values depend on configured CWL/CL and DDR4 turnaround timing.
        self.write_to_read_ck = write_to_read_ck
        self.read_to_write_ck = read_to_write_ck

    def _legal(self, req, ck):
        if self.last_cas_ck is None:
            return True
        required = TCCD_L_CK if req.group == self.last_cas_group else TCCD_S_CK
        if ck - self.last_cas_ck < required:
            return False
        if self.last_op is not None and req.op != self.last_op:
            turnaround = (self.write_to_read_ck if self.last_op == "write"
                          else self.read_to_write_ck)
            if ck - self.last_op_ck < turnaround:
                return False
        return True

    def _record(self, req, ck):
        self.last_cas_group = req.group
        self.last_cas_ck = ck
        self.last_op = req.op
        self.last_op_ck = ck

    def cycle(self, requests, *, ready=True, refresh=False, blocked=False):
        """Issue zero/one/two requests at offsets 0 and 4 CK in one slow cycle."""
        if not ready or refresh or blocked or not requests:
            self.cycle_index += 1
            return []
        base = self.cycle_index * CK_PER_SLOW_CYCLE
        first = requests[0]
        if not self._legal(first, base):
            self.cycle_index += 1
            return []
        slots = [Slot(base, first)]
        self._record(first, base)
        if len(requests) > 1:
            second = requests[1]
            ck = base + TCCD_S_CK
            # Four CK spacing is available only between opposite bank groups.
            if (second.group != first.group and second.op == first.op
                    and self._legal(second, ck)):
                slots.append(Slot(ck, second))
                self._record(second, ck)
        self.cycle_index += 1
        return slots


def run(requests, *, options=None, write_to_read_ck=0, read_to_write_ck=0):
    scheduler = DualSlotScheduler(write_to_read_ck=write_to_read_ck,
                                  read_to_write_ck=read_to_write_ck)
    pending = list(requests)
    issued = []
    options = options or {}
    for cycle in range(1000):
        if not pending:
            break
        slots = scheduler.cycle(pending, **options.get(cycle, {}))
        if slots:
            issued.extend(slots)
            pending = pending[len(slots):]
    if pending:
        raise AssertionError("scheduler failed to drain requests")
    return issued


class TestDualSlotSchedule(unittest.TestCase):
    def test_opposite_groups_sustain_distinct_bl8_write_payloads(self):
        a = bytes(range(32))
        b = bytes(255 - i for i in range(32))
        reqs = [Request("write", i % 2, i % 4, 7, i * 8,
                        (a, b)[i % 2], 1 << (i % 32), i) for i in range(20)]
        slots = run(reqs)
        self.assertEqual([s.ck for s in slots[:4]], [0, 4, 8, 12])
        self.assertEqual([s.request.payload for s in slots[:2]], [a, b])
        self.assertEqual([s.request.tag for s in slots], list(range(20)))
        self.assertEqual([s.request.mask for s in slots[:2]], [1, 2])
        for left, right in zip(slots, slots[1:]):
            required = TCCD_L_CK if left.request.group == right.request.group else TCCD_S_CK
            self.assertGreaterEqual(right.ck - left.ck, required)

    def test_same_group_falls_back_to_one_cas_each_slow_cycle(self):
        reqs = [Request("read", 0, i % 4, i // 4, i * 8, tag=i) for i in range(8)]
        slots = run(reqs)
        self.assertEqual([s.ck for s in slots], list(range(0, 64, 8)))

    def test_partial_pair_masks_and_row_boundary_keep_payload_identity(self):
        payloads = [bytes([i]) * BURST_BYTES for i in range(3)]
        reqs = [Request("write", 0, 0, 11, 1016, payloads[0], (1 << 32) - 1, 10),
                Request("write", 1, 0, 12, 0, payloads[1], 0, 11),
                Request("write", 0, 1, 12, 8, payloads[2], 0x55555555, 12)]
        slots = run(reqs)
        self.assertEqual([s.request.tag for s in slots], [10, 11, 12])
        self.assertEqual([s.request.payload for s in slots], payloads)
        self.assertEqual([s.request.mask for s in slots],
                         [(1 << 32) - 1, 0, 0x55555555])
        self.assertEqual(slots[2].ck, 8)

    def test_backpressure_refresh_and_write_to_read_turnaround_preserve_order(self):
        reqs = [Request("write", 0, 0, 0, 0, bytes([1]) * 32, tag=0),
                Request("write", 1, 0, 0, 8, bytes([2]) * 32, tag=1),
                Request("read", 0, 1, 0, 0, tag=2)]
        slots = run(reqs, options={0: {"ready": False}, 1: {"refresh": True},
                                   2: {"blocked": True}},
                    write_to_read_ck=16, read_to_write_ck=8)
        self.assertEqual([s.request.tag for s in slots], [0, 1, 2])
        self.assertEqual([s.ck for s in slots], [24, 28, 48])

    def test_direction_change_is_never_paired_in_one_cycle(self):
        reqs = [Request("write", 0, 0, 0, 0, bytes([1]) * 32, tag=0),
                Request("read", 1, 0, 0, 0, tag=1)]
        slots = run(reqs)
        self.assertEqual([s.request.tag for s in slots], [0, 1])
        self.assertGreaterEqual(slots[1].ck - slots[0].ck, 4)

    def test_physical_spacing_opposite_group_four_ck_and_same_group_eight(self):
        reqs = [Request("read", 0, 0, 0, 0, tag=0),
                Request("read", 1, 0, 0, 0, tag=1),
                Request("read", 0, 1, 0, 0, tag=2)]
        slots = run(reqs)
        self.assertEqual([s.ck for s in slots], [0, 4, 8])


if __name__ == "__main__":
    unittest.main()
