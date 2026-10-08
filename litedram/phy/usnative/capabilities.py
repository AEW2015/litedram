#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Configuration envelope for the experimental UltraScale native DDR PHY.

This describes configurations the source can represent. It does not qualify
package pin placement, vendor primitive legality, timing closure, calibration,
or memory operation.
"""

from dataclasses import dataclass


SUPPORTED_FAMILIES = ("ULTRASCALE", "ULTRASCALE_PLUS")
SUPPORTED_MEMTYPES = ("DDR4",)
SUPPORTED_DQ_WIDTHS = (16, 32, 64)
SUPPORTED_CONTROLLER_RATIOS = ("1:4", "1:8")


@dataclass(frozen=True)
class NativeConfiguration:
    family: str
    memtype: str
    databits: int
    controller_ratio: str


def validate_native_configuration(*, family, memtype, databits, controller_ratio):
    """Validate the source-level native-PHY configuration envelope.

    Supported configurations are returned as a normalized descriptor. A
    rejected configuration raises ``ValueError`` with the unsupported axis;
    callers must not treat acceptance as functional or hardware qualification.
    """
    family = str(family).upper()
    memtype = str(memtype).upper()
    if family not in SUPPORTED_FAMILIES:
        raise ValueError(f"Unsupported native SelectIO family: {family}")
    if memtype not in SUPPORTED_MEMTYPES:
        raise ValueError(f"Native PHY currently supports DDR4 only, got {memtype}")
    if databits not in SUPPORTED_DQ_WIDTHS:
        raise ValueError(f"Unsupported native DDR4 DQ width: x{databits}")
    if controller_ratio not in SUPPORTED_CONTROLLER_RATIOS:
        raise ValueError(f"Unsupported native controller ratio: {controller_ratio}")
    return NativeConfiguration(family, memtype, databits, controller_ratio)
