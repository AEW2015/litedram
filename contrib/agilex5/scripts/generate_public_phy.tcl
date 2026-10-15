package require qsys
source probe_config.tcl
create_system phy_probe
set_project_property DEVICE_FAMILY "Agilex 5"
set_project_property DEVICE A5ED065BB32AE6SR0
add_instance phy phylite_ph2
set_instance_parameter_value phy PHYLITE_NUM_GROUPS [expr {![string equal $mode "data"] ? 6 : 4}]
set_instance_parameter_value phy GUI_PHYLITE_MEM_CLK_FREQ_MHZ $freq
if {![info exists ref_freq]} {set ref_freq 150.0}
if {[string equal $ref_freq "auto"]} {
 set_instance_parameter_value phy GUI_PHYLITE_DEFAULT_REF_CLK_FREQ true
} else {
 set_instance_parameter_value phy GUI_PHYLITE_DEFAULT_REF_CLK_FREQ false
 set_instance_parameter_value phy GUI_PHYLITE_USER_REF_CLK_FREQ_MHZ $ref_freq
}
set_instance_parameter_value phy GUI_PHYLITE_PLL_REF_CLK_STD_ENUM PHYLITE_FM_PLL_REF_CLK_TRUE_DIFF_WITHOUT_OCT
set_instance_parameter_value phy GUI_PHYLITE_USE_DYNAMIC_RECONFIGURATION true
set_instance_parameter_value phy GUI_GENERATE_EX_DESIGN false
set_instance_parameter_value phy BYTE_IO_STANDARD IO_STANDARD_IOSTD_POD12
set_instance_parameter_value phy GENERATE_SDC_FILE true
set_instance_parameter_value phy GUI_GROUP_DATA_PINS_CUSTOM true
set_instance_parameter_value phy GUI_RZQ_IDX 38
# Logical byte order; data indices follow verified physical IO96 positions.
set pins [list "48-51,56-59" "0-3,8-11" "60-63,68-71" "72-75,80-83"]
for {set g 0} {$g<4} {incr g} {
 foreach {suffix value} {
  PIN_TYPE BIDIR PIN_WIDTH 8 DDR_SDR_MODE DDR DATA_CONFIG SGL_ENDED
  STROBE_CONFIG DIFFERENTIAL USE_INTERNAL_CAPTURE_STROBE false
  USE_OUTPUT_STROBE true DEFAULT_OCT false
  RX_TERMINATION RT_60_OHM_CAL TX_TERMINATION SERIES_34_OHM_CAL
  GENERATE_INPUT_CONSTRAINT false GENERATE_OUTPUT_CONSTRAINT false
 } {set_instance_parameter_value phy GUI_GROUP_${g}_${suffix} $value}
 set_instance_parameter_value phy GUI_GROUP_${g}_DATA_PINS [lindex $pins $g]
}
if {![string equal $mode "data"]} {
 # The actual board's commands include PHY Lite's candidate strobe pins.
 # This deliberately tests the fixed board routing without moving signals.
 set command_groups {4 "12-15,18,20-21,23" 8 5 "24-27,30-35,39-43,45-47" 18}
 if {[string equal $mode "hybrid"]} {
  # Public output strobes carry ODT and A4; A5 remains a fabric output.
  set command_groups {4 "12-15,18,20-21,23" 8 5 "24-27,30-35,39-43,45-47" 18}
 }
 foreach {g pinlist width} $command_groups {
  foreach {suffix value} {
   PIN_TYPE OUTPUT DDR_SDR_MODE DDR DATA_CONFIG SGL_ENDED
   STROBE_CONFIG SINGLE_ENDED USE_OUTPUT_STROBE true DEFAULT_OCT false
   TX_TERMINATION SERIES_34_OHM_CAL GENERATE_OUTPUT_CONSTRAINT false
  } {set_instance_parameter_value phy GUI_GROUP_${g}_${suffix} $value}
  set_instance_parameter_value phy GUI_GROUP_${g}_PIN_WIDTH $width
  set_instance_parameter_value phy GUI_GROUP_${g}_DATA_PINS $pinlist
 }
}
sync_sysinfo_parameters
set interfaces [get_instance_interfaces phy]
puts "INTERFACES $interfaces"
if {[llength $interfaces]>0} {
foreach iface {ref_clk_clock_sink reset_reset_sink core_clk_out lock_conduit_end rzq_in} {
 set type conduit
 set direction end
 if {[string equal $iface "ref_clk_clock_sink"]} {set type clock;set direction sink}
 if {[string equal $iface "reset_reset_sink"]} {set type reset;set direction sink}
 if {[string equal $iface "core_clk_out"]} {set type clock;set direction source}
 add_interface $iface $type $direction
 set_interface_property $iface EXPORT_OF phy.$iface
}
for {set g 0} {$g<[get_instance_parameter_value phy PHYLITE_NUM_GROUPS]} {incr g} {
 foreach suffix {io_interface_conduit_end core_interface_conduit_end} {
  set iface group_${g}_${suffix}
  add_interface $iface conduit end
  set_interface_property $iface EXPORT_OF phy.$iface
 }
}
# Public PHY register access through vendor-provided AXI-Lite calibration IP.
add_instance cal emif_io96b_cal
set_instance_parameter_value cal NUM_CALBUS_PERIPHS 1
set_instance_parameter_value cal NUM_CALBUS_PLLS 0
set_instance_parameter_value cal PORT_S_AXIL_MODE FABRIC
add_connection cal.calbus_periph_0 phy.calbus_periph
foreach {iface type source} {cal_clk clock s0_axi4lite_clk cal_reset reset s0_axi4lite_rst_n cal_axi axi4lite s0_axi4lite} {
 add_interface $iface $type end
 set_interface_property $iface EXPORT_OF cal.$source
}
}
foreach p {PHYLITE_RATE_ENUM PHYLITE_IN_RATE PHYLITE_OUT_RATE PHYLITE_CORE_CLK_FREQ_MHZ_DISPLAY PHYLITE_MEM_CLK_FREQ_MHZ PHYLITE_REF_CLK_FREQ_MHZ INHERENT_READ_LATENCY} {
 puts "CONTRACT $p=[get_instance_parameter_value phy $p]"
}

save_system phy_probe.qsys
