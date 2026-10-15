# Placement probe: no external DDR timing qualification is claimed.
create_clock -name board_clk -period 10.000 [get_ports clk]
create_clock -name refclk -period 3.750000000 [get_ports ref_clk]
set_false_path -from [get_ports reset_n]
set_false_path -to [get_ports led]

# Only the first stage of the two-flop PHY-lock status synchronizer is asynchronous.
if {[get_collection_size [get_registers -nowarn {locked_sync[0]}]] > 0} {
 set_false_path -to [get_registers {locked_sync[0]}]
}
# Controller /4 is constrained below from its physical input clock.

# Native /4 clock, with the PHY core as its physical source.

