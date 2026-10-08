#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Full-rate DFI writes must preserve adjacent, distinct controller bursts."""

import unittest

from migen import *

from litedram.phy.dfi import Interface, DFIRateConverter
from test.phy_common import run_simulation


class FullBurstDFIDUT(Module):
    def __init__(self):
        self.phy_dfi = Interface(addressbits=17, bankbits=3, nranks=1,
            databits=64, nphases=4)
        self.submodules.converter = DFIRateConverter(self.phy_dfi,
            clkdiv="sys", clk="sys2x", ratio=2, serdes_reset_cnt=-1,
            preserve_throughput=True)
        self.controller_dfi = self.converter.dfi


class TestFullBurstDFI(unittest.TestCase):
    clocks = {"sys": (8, 3), "sys2x": (4, 1)}

    def test_consecutive_distinct_write_transactions_keep_both_slots(self):
        dut = FullBurstDFIDUT()
        controller = dut.controller_dfi
        phy = dut.phy_dfi
        first_token = 16
        transaction_count = 24

        def drive_transactions():
            for token in range(first_token + transaction_count * 2):
                for phase, dfi_phase in enumerate(controller.phases):
                    # The token changes every controller cycle and the phase
                    # identifies which half of the full-rate word was sent.
                    value = (token << 8) | phase
                    yield dfi_phase.wrdata.eq(value)
                    yield dfi_phase.wrdata_mask.eq(((token << 3) | phase) & 0xff)
                    yield dfi_phase.wrdata_en.eq(1)
                yield

        def observe_fast_transactions():
            # Ignore serializer startup, then observe complete transaction
            # groups while the driver continues to change data every sys edge.
            for _ in range(first_token * 2 + 8):
                yield
            observed_tokens = []
            for _ in range(transaction_count * 2):
                phase_values = []
                phase_masks = []
                phase_enables = []
                for phase in phy.phases:
                    phase_values.append((yield phase.wrdata))
                    phase_masks.append((yield phase.wrdata_mask))
                    phase_enables.append((yield phase.wrdata_en))
                token = phase_values[0] >> 8
                low_slot = [token << 8 | phase for phase in range(4)]
                high_slot = [token << 8 | (phase + 4) for phase in range(4)]
                self.assertIn(phase_values, (low_slot, high_slot))
                self.assertEqual(phase_enables, [1] * 4)
                if phase_values == low_slot:
                    self.assertEqual(phase_masks,
                        [((token << 3) | phase) & 0xff for phase in range(4)])
                else:
                    self.assertEqual(phase_masks,
                        [((token << 3) | (phase + 4)) & 0xff for phase in range(4)])
                observed_tokens.append(token)
                yield

            # Each controller word contributes slots 0 and 1 on consecutive
            # fast edges; distinct tokens prove the converter did not replay,
            # drop, or overwrite a queued burst while the next one arrived.
            runs = []
            for token in observed_tokens:
                if not runs or runs[-1][0] != token:
                    runs.append([token, 1])
                else:
                    runs[-1][1] += 1
            self.assertGreaterEqual(len(runs), transaction_count - 2)
            for _, count in runs[1:-1]:
                self.assertEqual(count, 2)
            self.assertEqual([token for token, _ in runs],
                list(range(runs[0][0], runs[0][0] + len(runs))))

        run_simulation(dut,
            {"sys": [drive_transactions()], "sys2x": [observe_fast_transactions()]},
            clocks=self.clocks)


if __name__ == "__main__":
    unittest.main()
