"""Two-stage, hard-PHY-group-local read-valid diagnostic pipeline."""

from functools import reduce
from operator import or_

from migen import If, Module, ResetSignal, Signal


class PhyDiagnosticValidPipeline(Module):
    """Capture every lane-valid locally, then register one OR per group.

    The combinational ``valid`` output is the OR of the registered group
    summaries. Relative to registering one global OR, this deliberately adds
    one diagnostic-event cycle. This pipeline does not participate in native
    DFI read data, DFI valid, or PHY capture-mask timing.
    """

    def __init__(self, group_valid):
        if not group_valid:
            raise ValueError("at least one group-valid signal is required")
        self.lane_valid_registered = []
        self.group_valid_registered = []
        for index, valid in enumerate(group_valid):
            lanes = Signal(len(valid), reset=0,
                name=f"phy_diagnostic_group_{index}_lane_valid_registered")
            summary = Signal(reset=0,
                name=f"phy_diagnostic_group_{index}_valid_registered")
            # Keep each capture point beside its hard-PHY validity outputs,
            # and prevent retiming from moving the wide reduction before it.
            for signal in (lanes, summary):
                signal.attr.add("preserve")
                signal.attr.add("no_retiming")
            self.lane_valid_registered.append(lanes)
            self.group_valid_registered.append(summary)
            self.sync += If(ResetSignal(),
                lanes.eq(0), summary.eq(0)
            ).Else(
                lanes.eq(valid), summary.eq(lanes != 0)
            )
        self.valid = Signal(name="phy_diagnostic_valid_registered")
        self.comb += self.valid.eq(reduce(or_, self.group_valid_registered))
