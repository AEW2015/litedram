// Memory-only dual-bank prototype. No accelerator or flat-address remapping.
module dual_ddr_memory_test_native2133_v1(
 input wire clk,reset_n,ref_clk,rzq,ref_clk3,rzq3,
 inout wire clock_scl,clock_sda,output wire led,
 inout wire [31:0] dq,dq3,inout wire [3:0] dqs_p,dqs_n,dqs3_p,dqs3_n,
 output wire [16:0] a,a3,output wire [1:0] ba,bg,ba3,bg3,
 output wire ck_p,ck_n,cke,cs_n,act_n,odt,par,dram_reset_n,
 output wire ck3_p,ck3_n,cke3,cs3_n,act3_n,odt3,par3,dram_reset3_n
);
 wire [1:0] core_clk,locked,core_rst;
 wire [2:0] core_reset_sync0,core_reset_sync1;
 wire init_reset;
 // One authoritative reset-release IP lives inside system_inst.
 wire fabric_reset_n=reset_n & ~init_reset;
 wire [1:0] clock_i2c_low;
 assign clock_scl=clock_i2c_low[0]?1'b0:1'bz;
 assign clock_sda=clock_i2c_low[1]?1'b0:1'bz;
 altsource_probe #(.probe_width(2),.source_width(2),.instance_id("CI2C"),
 .source_initial_value("0"),.sync_source_clk("NO")) clock_i2c_debug(
 .probe({clock_sda,clock_scl}),.source(clock_i2c_low),.source_clk(1'b0),.source_ena(1'b1));
 wire [1:0] debug_control,debug3_control;
 wire force_cpu_reset=debug_control[0],force_phy_reset=debug_control[1],force_phy3_reset=debug3_control[1];
 reg [27:0] board_ticks=0,phy_ticks=0,phy3_ticks=0;
 always @(posedge clk)board_ticks<=board_ticks+1;
 always @(posedge core_clk[0])phy_ticks<=phy_ticks+1;
 always @(posedge core_clk[1])phy3_ticks<=phy3_ticks+1;
 altsource_probe #(.probe_width(32),.source_width(2),.instance_id("A5TOPDBG"),.sync_source_clk("NO")) top_debug(
 .probe({phy_ticks[27:16],board_ticks[27:16],force_cpu_reset,core_clk[0],core_reset_sync0[1:0],core_rst[0],locked[0],init_reset,reset_n}),
 .source(debug_control),.source_clk(1'b0),.source_ena(1'b1));
 altsource_probe #(.probe_width(32),.source_width(2),.instance_id("A5D3DBG"),.sync_source_clk("NO")) bank3_debug(
 .probe({phy3_ticks[27:16],board_ticks[27:16],1'b0,core_clk[1],core_reset_sync1[1:0],core_rst[1],locked[1],init_reset,reset_n}),
 .source(debug3_control),.source_clk(1'b0),.source_ena(1'b1));
 (* ASYNC_REG="TRUE",preserve,dont_merge,no_retiming,altera_attribute="-name SYNCHRONIZER_IDENTIFICATION FORCED_IF_ASYNCHRONOUS" *) reg [2:0] locked_status_sync=0;
 always @(posedge clk)locked_status_sync<={locked_status_sync[1:0],locked[0]};
 wire [1:0][1341:0] calbus;wire [1:0][1314:0] calread;wire [1:0][32767:0] caltable;
 wire [11:0] csr_address;wire csr_read,csr_write,csr_waitrequest,csr_readdatavalid;
 wire [31:0] csr_writedata,csr_readdata;wire [3:0] leds;assign led=leds[0];
 dual_ddr_control_system_r7 system_inst(.ninit_done(init_reset),.clk_clk(clk),.reset_reset_n(reset_n&~force_cpu_reset),
 .core_clk_clk(core_clk[0]),.leds_export(leds),.status_export(locked_status_sync[2]),
 .csr_address,.csr_read,.csr_write,.csr_writedata,.csr_readdata,.csr_readdatavalid,.csr_waitrequest,
 .calbus_calbus(calbus[0]),.calbus_calbus_readdata(calread[0]),.calbus_calbus_param_table(caltable[0]),
 .calbus3_calbus(calbus[1]),.calbus3_calbus_readdata(calread[1]),.calbus3_calbus_param_table(caltable[1]));
 wire [1:0][11:0] b_address,b_config_address;wire [1:0] b_read,b_write,b_waitrequest,b_readdatavalid;
 wire [1:0][31:0] b_writedata,b_readdata;
 wire bank_select,router_busy,router_fault;
 wire common_mailbox_reset_n=fabric_reset_n&locked[0]&locked[1];
 (* ASYNC_REG="TRUE",preserve,dont_merge,no_retiming,altera_attribute="-name SYNCHRONIZER_IDENTIFICATION FORCED_IF_ASYNCHRONOUS" *) reg [2:0] mailbox_reset_sync0=0,mailbox_reset_sync1=0;
 always @(posedge core_clk[0] or negedge common_mailbox_reset_n)begin
  if(!common_mailbox_reset_n)mailbox_reset_sync0<=0;else mailbox_reset_sync0<={mailbox_reset_sync0[1:0],1'b1};
 end
 always @(posedge core_clk[1] or negedge common_mailbox_reset_n)begin
  if(!common_mailbox_reset_n)mailbox_reset_sync1<=0;else mailbox_reset_sync1<={mailbox_reset_sync1[1:0],1'b1};
 end
 wire [1:0] mailbox_rst={~mailbox_reset_sync1[2],~mailbox_reset_sync0[2]};
 dual_ddr_csr_router_fast_v11 u_router(.clk(core_clk[0]),.rst(~router_metadata_reset_sync[2]),.mailbox_rst(mailbox_rst[0]),
 .av_address(csr_address),.av_read(csr_read),.av_write(csr_write),.av_writedata(csr_writedata),
 .av_waitrequest(csr_waitrequest),.av_readdatavalid(csr_readdatavalid),.av_readdata(csr_readdata),
 .bank_clk(core_clk),.bank_rst(mailbox_rst),.bank_locked(locked),.owner_active(2'b00),.bank_select,
 .busy(router_busy),.fault(router_fault),.b_address,.b_config_address,.b_read,.b_write,.b_writedata,.b_waitrequest,.b_readdatavalid,.b_readdata);
 dual_ddr_bank0_path_native2133_v1 bank0(.ref_clk,.phy_reset_n(reset_n&~force_phy_reset),.fabric_reset_n,.rzq,
 .core_clk(core_clk[0]),.locked(locked[0]),.core_rst(core_rst[0]),.core_reset_sync(core_reset_sync0),
 .calbus(calbus[0]),.calread(calread[0]),.caltable(caltable[0]),
 .csr_address(b_address[0]),.csr_config_address(b_config_address[0]),.csr_read(b_read[0]),.csr_write(b_write[0]),.csr_writedata(b_writedata[0]),
 .csr_readdata(b_readdata[0]),.csr_readdatavalid(b_readdatavalid[0]),.csr_waitrequest(b_waitrequest[0]),
 .dq,.dqs_p,.dqs_n,.a,.ba,.bg,.ck_p,.ck_n,.cke,.cs_n,.act_n,.odt,.par,.dram_reset_n);
 dual_ddr_bank1_path_native2133_v1 bank1(.ref_clk(ref_clk3),.phy_reset_n(reset_n&~force_phy3_reset),.fabric_reset_n,.rzq(rzq3),
 .core_clk(core_clk[1]),.locked(locked[1]),.core_rst(core_rst[1]),.core_reset_sync(core_reset_sync1),
 .calbus(calbus[1]),.calread(calread[1]),.caltable(caltable[1]),
 .csr_address(b_address[1]),.csr_config_address(b_config_address[1]),.csr_read(b_read[1]),.csr_write(b_write[1]),.csr_writedata(b_writedata[1]),
 .csr_readdata(b_readdata[1]),.csr_readdatavalid(b_readdatavalid[1]),.csr_waitrequest(b_waitrequest[1]),
 .dq(dq3),.dqs_p(dqs3_p),.dqs_n(dqs3_n),.a(a3),.ba(ba3),.bg(bg3),
 .ck_p(ck3_p),.ck_n(ck3_n),.cke(cke3),.cs_n(cs3_n),.act_n(act3_n),.odt(odt3),.par(par3),.dram_reset_n(dram_reset3_n));
 // Local metadata reset distribution: do not borrow the controller's massive
 // final reset net. Async assertion survives native-clock loss, sync release.
 wire router_metadata_reset_n=fabric_reset_n&locked[0];
 (* ASYNC_REG="TRUE",preserve,dont_merge,no_retiming,
 altera_attribute="-name SYNCHRONIZER_IDENTIFICATION FORCED_IF_ASYNCHRONOUS" *) reg [2:0] router_metadata_reset_sync=0;
 always @(posedge core_clk[0] or negedge router_metadata_reset_n)begin
  if(!router_metadata_reset_n)router_metadata_reset_sync<=0;
  else router_metadata_reset_sync<={router_metadata_reset_sync[1:0],1'b1};
 end
endmodule
