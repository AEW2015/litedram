#
# SPDX-License-Identifier: BSD-2-Clause

"""Directed regression for stale bank refresh grants."""

import unittest

from litex.gen.sim import run_simulation

from test.test_multiplexer import MultiplexerDUT


class TestRefreshGuard(unittest.TestCase):
    def test_stale_grants_do_not_reenter_refresh(self):
        for registered in (False, True):
            with self.subTest(registered_request=registered):
                dut = MultiplexerDUT(controller_settings=dict(
                    with_registered_refresh_request=registered))

                def generator():
                    # Simulate the tail of one refresh. The grants remain high
                    # after the request has dropped, as they can in the real
                    # bank machines for a cycle or more.
                    for bm in dut.bank_machines:
                        yield bm.refresh_gnt.eq(1)
                    yield dut.refresher.cmd.valid.eq(1)
                    yield
                    for _ in range(4):
                        if (yield from dut.fsm_state()) == "REFRESH":
                            break
                        yield
                    self.assertEqual((yield from dut.fsm_state()), "REFRESH")

                    # The refresher's last beat returns the mux to READ. Keep
                    # grants asserted while request deassertion propagates.
                    yield dut.refresher.cmd.last.eq(1)
                    yield dut.refresher.cmd.valid.eq(0)
                    yield
                    yield
                    yield dut.refresher.cmd.last.eq(0)
                    for _ in range(4):
                        self.assertEqual((yield from dut.fsm_state()), "READ")
                        yield

                run_simulation(dut, generator())
