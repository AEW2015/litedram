# SPDX-License-Identifier: BSD-2-Clause
"""Cycle-level tests for the CK-positioned dual-slot Migen scheduler."""
import unittest
from functools import reduce
from operator import or_

from migen import Case, ClockDomain, If, Module, Signal
from litex.gen.sim import run_simulation

from litedram.core.dual_slot import DualSlotScheduler


def request(tag, group, *, bank=0, row=0, col=0, write=True):
    return {
        "tag": tag, "group": group, "bank": bank, "row": row, "col": col,
        "write": int(write), "data": (tag + 1) * 0x0101010101010101,
        "mask": ((1 << 32) - 1) ^ (1 << (tag % 32)),
    }


def input_case(dut, source, position, queue):
    cases = {}
    for queue_index, item in enumerate(queue):
        cases[queue_index] = [
            dut.req_valid[source].eq(1),
            dut.req_group[source].eq(item["group"]),
            dut.req_bank[source].eq(item["bank"]),
            dut.req_row[source].eq(item["row"]),
            dut.req_col[source].eq(item["col"]),
            dut.req_write[source].eq(item["write"]),
            dut.req_data[source].eq(item["data"]),
            dut.req_mask[source].eq(item["mask"]),
            dut.req_tag[source].eq(item["tag"]),
        ]
    cases["default"] = [dut.req_valid[source].eq(0)]
    return Case(position, cases)


class TestDualSlotRTL(unittest.TestCase):
    def simulate_queues(self, queues, *, stalled_cycles=()):
        dut = DualSlotScheduler()
        top = Module()
        top.clock_domains.cd_sys = ClockDomain()
        top.clock_domains.cd_sample = ClockDomain()
        top.submodules.dut = dut
        positions = []
        producer_advances = []
        for i, queue in enumerate(queues):
            position = Signal(max= max(2, len(queue) + 1), name=f"source{i}_index")
            positions.append(position)
            top.comb += input_case(dut, i, position, queue)
            # The producer advances only when its own ready/valid handshake
            # occurs, preserving the request throughout scheduler stalls.
            producer_advances.append(If(dut.req_ready[i],
                If(position < len(queue), position.eq(position + 1))
            ))

        cycle = Signal(16, name="cycle")
        top.sync += cycle.eq(cycle + 1)
        if stalled_cycles:
            top.comb += dut.issue_enable.eq(
                ~reduce(or_, [cycle == stalled for stalled in stalled_cycles]))

        top.sync += producer_advances

        collected = []

        def monitor():
            for cycle_number in range(100):
                for slot in range(2):
                    if (yield dut.slot_valid[slot]):
                        fields = {}
                        for key, signals in {
                            "tag": dut.slot_tag, "group": dut.slot_group,
                            "bank": dut.slot_bank, "row": dut.slot_row,
                            "col": dut.slot_col, "write": dut.slot_write,
                            "data": dut.slot_data, "mask": dut.slot_mask,
                            "offset": dut.slot_ck_offset,
                        }.items():
                            fields[key] = (yield signals[slot])
                        ck = cycle_number * 8 + fields["offset"]
                        collected.append((ck, fields))
                yield
                done = True
                for i in range(2):
                    done &= (yield positions[i]) == len(queues[i])
                if done:
                    break
            else:
                self.fail("dual-slot scheduler did not drain input queues")

        run_simulation(top, {"sample": monitor()}, clocks={"sys": 10, "sample": (10, 3)})
        return dut, collected

    def test_alternating_groups_sustain_ck_zero_four_eight_twelve(self):
        q0 = [request(i, 0, bank=i % 4, row=9, col=i * 8) for i in range(12)]
        q1 = [request(i + 100, 1, bank=i % 4, row=10, col=i * 8)
              for i in range(12)]
        _, accepted = self.simulate_queues([q0, q1])
        self.assertEqual(len(accepted), 24, [item["tag"] for _, item in accepted])
        self.assertEqual([ck for ck, _ in accepted[:8]], [0, 4, 8, 12, 16, 20, 24, 28])
        self.assertEqual([item["tag"] for _, item in accepted[:4]], [0, 100, 1, 101])
        self.assertEqual(len({item["tag"] for _, item in accepted}), 24)
        for start in range(0, len(accepted), 2):
            pair = [item for _, item in accepted[start:start+2]]
            self.assertEqual({item["group"] for item in pair}, {0, 1})
            self.assertNotEqual(pair[0]["data"], pair[1]["data"])
            self.assertNotEqual(pair[0]["mask"], pair[1]["mask"])

    def test_same_group_accepts_one_per_cycle_and_round_robins_sources(self):
        q0 = [request(i, 0, bank=i % 4) for i in range(6)]
        q1 = [request(i + 100, 0, bank=(i + 1) % 4) for i in range(6)]
        _, accepted = self.simulate_queues([q0, q1])
        self.assertEqual(len(accepted), 12)
        self.assertEqual([ck for ck, _ in accepted], list(range(0, 96, 8)))
        self.assertEqual([item["tag"] for _, item in accepted[:4]], [0, 100, 1, 101])

    def test_issue_backpressure_holds_then_drains_every_command_once(self):
        q0 = [request(i, 0) for i in range(4)]
        q1 = [request(i + 100, 1) for i in range(4)]
        _, accepted = self.simulate_queues([q0, q1], stalled_cycles=(1, 2, 5))
        tags = [item["tag"] for _, item in accepted]
        self.assertEqual(len(tags), 8)
        self.assertCountEqual(tags, list(range(4)) + list(range(100, 104)))
        self.assertEqual(len(tags), len(set(tags)))
        self.assertFalse(any(ck // 8 in (1, 2, 5) for ck, _ in accepted))

    def test_read_and_write_are_not_paired_in_the_same_slow_cycle(self):
        q0 = [request(1, 0, write=True)]
        q1 = [request(2, 1, write=False)]
        _, accepted = self.simulate_queues([q0, q1])
        self.assertEqual(len(accepted), 2)
        self.assertGreaterEqual(accepted[1][0], accepted[0][0] + 8)
        self.assertEqual({item["write"] for _, item in accepted}, {0, 1})


if __name__ == "__main__":
    unittest.main()
