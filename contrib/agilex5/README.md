# Agilex 5 second-DDR-bank source handoff

This experimental source overlay supports investigation of **fabric bank 3B** on
the Agilex 5 E065B Modular ES kit, user-reported SOM Rev B. Bank 2B is logical bank
0; bank 3B is logical bank 1. Both interfaces are x32, 8 GiB each.

| Configuration | Recorded outcome |
|---|---|
| DDR1600 | Both banks passed nominal full-capacity, sparse/isolation and concurrent diagnostics. |
| DDR2133.333, memory v14 | Scoped internal timing audit passed; bank 3B failed initial hardware MPR with six mismatches. Bank 2B passed its local diagnostic. |
| DDR2666.667 | Source/simulation prototype only. Selected vendor generation rejects CK1333.333 MHz above its 150–1250 MHz GUI range; legacy transformations are unsupported. |

The DDR2133 mismatch maps to **DQ8–DQ15, group 1/lane 4**. Its physical cause has
not been established. Improving internal setup timing alone does not establish
that the MPR failure is fixed. See [investigation notes](BANK3B_HANDOFF.md) and the
small original records in [evidence/](evidence/).

The review branch starts at the recorded LiteDRAM dependency commit
`a1da8cb4fb7901aa6019330496d17c7c91ccc3ef`, preserving generator compatibility.
Existing fork branches are unchanged.

## Source scope

- `litedram_phy/`: consumed custom PHY/controller support modules.
- `integration/dual-ddr-v1/`: DDR1600 and dense DDR2133/2666 generators, compact
  legacy reset policy, handwritten memory RTL, calibration and diagnostic sources.
- `source/`: relocated Python generator seed and expected controller hashes.
- `prepare_bank3_phy.py`: namespacing of freshly generated second-bank PHY files.
- `constraints/`: selected original QSF/SDC **configuration snapshots**.

This source handoff excludes generated RTL, vendor snapshots, toolchains,
bitstreams, BSPs, databases, large reports and model files. The QSF snapshots
retain paths from the original larger project; **this overlay is not a complete,
independently compile-ready Quartus board project**. Existing historical helper
scripts may require original artifacts; only the clean source replay below is
claimed here. The notes index historical evidence paths that are not all bundled.

## Clean controller replay

From `contrib/agilex5` in this LiteDRAM fork:

```sh
python3 bootstrap_deps.py
python3 -m venv .venv
.venv/bin/python -m pip install -e third_party/migen -e third_party/litex -e ../..
.venv/bin/python tests/test_primitive_row_hit.py -v
.venv/bin/python verify_controllers.py
```

The bootstrap fetches pinned Migen/LiteX commits, applies the small Migen
simulation-condition patch, and links this LiteDRAM checkout. It does not reset
existing dependency checkouts. All fetched sources and generated results stay
gitignored. A repeated replay needs a new `--output generated/another-run`.

The replay was run from the compact overlay: all three regenerated controllers
matched their preserved RTL hashes byte-for-byte. DDR1600's replay is the base
controller before its separate reset-distribution stage; DDR2133/2666 include
the repaired reset/return/CSR layers. This verifies source portability, **not new
Quartus timing or hardware performance**. The registered-row-hit regression also
passed its three tests. See [verification record](evidence/source-replay.json).

## PHY generation and timing work

Use Quartus Pro 26.1.1 and device `A5ED065BB32AE6SR0`. The selected DDR2133 `.ip`
descriptors are under `phy-native2133-v1/source-r2` and
`phy-native2133-bank3-r3/source` inside `integration/dual-ddr-v1`. Generate their
synthesis files locally with `qsys-generate DESCRIPTOR.ip --synthesis=VERILOG
--family="Agilex 5" --part=A5ED065BB32AE6SR0`. The tool may write beside the
descriptor even when an output-directory option is supplied.

Then run `prepare_bank3_phy.py --bank0 BANK0_TREE --bank3-raw BANK3_RAW_TREE
--output generated/bank3`. Its exact comparison with the preserved selected
bank-3 source files is recorded in
[bank3_namespace_verification.json](evidence/bank3_namespace_verification.json).
That comparison reused existing generated inputs; no new vendor IP generation
or Quartus compile was performed for this publication.

Start with the DDR2133 dense controller and bank-3 mapping, retain the passing
DDR1600 configuration, and compare fresh MPR captures before changing receiver
timing. Record all setup/hold corners, clock exceptions and external-I/O limits;
repeat both-bank full-capacity/concurrent diagnostics before claiming a pass.

The attached board is running a separate chat session. This handoff does not
transfer its hardware ownership. Coordinate any programming/reset/clock changes
separately; source generation and review can proceed independently.
