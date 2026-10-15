module dual_ddr_bank1_path_r8(
input wire ref_clk,phy_reset_n,fabric_reset_n,rzq,
 output wire core_clk,locked,core_rst,(* ASYNC_REG="TRUE",preserve,dont_merge,no_retiming,altera_attribute="-name SYNCHRONIZER_IDENTIFICATION FORCED_IF_ASYNCHRONOUS" *) output reg [2:0] core_reset_sync,
 input wire [1341:0] calbus,output wire [1314:0] calread,output wire [32767:0] caltable,
 input wire [11:0] csr_address,input wire csr_read,csr_write,input wire [31:0] csr_writedata,
 output wire [31:0] csr_readdata,output wire csr_readdatavalid,csr_waitrequest,
 inout wire [31:0] dq,inout wire [3:0] dqs_p,dqs_n,output wire [16:0] a,
 output wire [1:0] ba,bg,output wire ck_p,ck_n,cke,cs_n,act_n,odt,par,dram_reset_n
);
wire [63:0] cmd4; wire [143:0] cmd5;
wire [7:0] cmd4_strobe,cmd5_strobe; wire a5gpio;
assign a[5]=a5gpio;
wire [63:0] g0_out,g0_in; wire [3:0] g0_valid,g0_read_en,g0_oe,g0_strobe_oe; wire [7:0] g0_strobe;
wire [63:0] g1_out,g1_in; wire [3:0] g1_valid,g1_read_en,g1_oe,g1_strobe_oe; wire [7:0] g1_strobe;
wire [63:0] g2_out,g2_in; wire [3:0] g2_valid,g2_read_en,g2_oe,g2_strobe_oe; wire [7:0] g2_strobe;
wire [63:0] g3_out,g3_in; wire [3:0] g3_valid,g3_read_en,g3_oe,g3_strobe_oe; wire [7:0] g3_strobe;

 wire core_reset_n=fabric_reset_n & locked;
 always @(posedge core_clk or negedge core_reset_n)begin
  if(!core_reset_n)core_reset_sync<=0;else core_reset_sync<={core_reset_sync[1:0],1'b1};
 end
 assign core_rst=~core_reset_sync[2];
litedram_external_controller u_controller(.c_request(1'b0),.c_granted(),.c_ready(),
.c_drain_req(1'b0),.c_drain_done(),
.c_cmd_valid(1'b0),.c_cmd_ready(),.c_cmd_we(1'b0),.c_cmd_address(28'b0),
.c_wdata_valid(1'b0),.c_wdata_ready(),.c_wdata_data(256'b0),.c_wdata_we(32'b0),
.c_rdata_valid(),.c_rdata_ready(1'b1),.c_rdata_data(),.sys_clk(core_clk),.sys_rst(core_rst),.locked(locked),.cmd4(cmd4),.cmd5(cmd5),.cmd4_strobe(cmd4_strobe),.cmd5_strobe(cmd5_strobe),.a5gpio(a5gpio),
.csr_address(csr_address),.csr_read(csr_read),.csr_write(csr_write),.csr_writedata(csr_writedata),
.csr_readdata(csr_readdata),.csr_readdatavalid(csr_readdatavalid),.csr_waitrequest(csr_waitrequest),
.g0_data_out(g0_out),.g0_data_in(g0_in),.g0_valid(g0_valid),.g0_read_en(g0_read_en),
.g0_oe(g0_oe),.g0_strobe(g0_strobe),.g0_strobe_oe(g0_strobe_oe),
.g1_data_out(g1_out),.g1_data_in(g1_in),.g1_valid(g1_valid),.g1_read_en(g1_read_en),
.g1_oe(g1_oe),.g1_strobe(g1_strobe),.g1_strobe_oe(g1_strobe_oe),
.g2_data_out(g2_out),.g2_data_in(g2_in),.g2_valid(g2_valid),.g2_read_en(g2_read_en),
.g2_oe(g2_oe),.g2_strobe(g2_strobe),.g2_strobe_oe(g2_strobe_oe),
.g3_data_out(g3_out),.g3_data_in(g3_in),.g3_valid(g3_valid),.g3_read_en(g3_read_en),
.g3_oe(g3_oe),.g3_strobe(g3_strobe),.g3_strobe_oe(g3_strobe_oe));
dual3_phy_probe_phy u_phy(.ref_clk(ref_clk),.reset_n(phy_reset_n),.interface_locked(locked),.core_clk_out(core_clk),.rzq(rzq),
.periph_calbus(calbus),.periph_calbus_readdata(calread),.periph_calbus_param_table(caltable),
.group_0_data_from_core(g0_out),.group_0_data_to_core(g0_in),.group_0_rdata_valid(g0_valid),.group_0_rdata_en(g0_read_en),
.group_0_oe_from_core(g0_oe),.group_0_strobe_from_core(g0_strobe),.group_0_strobe_out_en(g0_strobe_oe),
.group_0_data_io({dq[1], dq[5], dq[3], dq[7], dq[4], dq[6], dq[0], dq[2]}),.group_0_strobe_io(dqs_p[0]),.group_0_strobe_io_n(dqs_n[0]),
.group_1_data_from_core(g1_out),.group_1_data_to_core(g1_in),.group_1_rdata_valid(g1_valid),.group_1_rdata_en(g1_read_en),
.group_1_oe_from_core(g1_oe),.group_1_strobe_from_core(g1_strobe),.group_1_strobe_out_en(g1_strobe_oe),
.group_1_data_io({dq[9], dq[15], dq[11], dq[8], dq[10], dq[13], dq[14], dq[12]}),.group_1_strobe_io(dqs_p[1]),.group_1_strobe_io_n(dqs_n[1]),
.group_2_data_from_core(g2_out),.group_2_data_to_core(g2_in),.group_2_rdata_valid(g2_valid),.group_2_rdata_en(g2_read_en),
.group_2_oe_from_core(g2_oe),.group_2_strobe_from_core(g2_strobe),.group_2_strobe_out_en(g2_strobe_oe),
.group_2_data_io({dq[23], dq[22], dq[21], dq[20], dq[19], dq[18], dq[17], dq[16]}),.group_2_strobe_io(dqs_p[2]),.group_2_strobe_io_n(dqs_n[2]),
.group_3_data_from_core(g3_out),.group_3_data_to_core(g3_in),.group_3_rdata_valid(g3_valid),.group_3_rdata_en(g3_read_en),
.group_3_oe_from_core(g3_oe),.group_3_strobe_from_core(g3_strobe),.group_3_strobe_out_en(g3_strobe_oe),
.group_3_data_io({dq[31], dq[30], dq[29], dq[28], dq[27], dq[26], dq[25], dq[24]}),.group_3_strobe_io(dqs_p[3]),.group_3_strobe_io_n(dqs_n[3]),
.group_4_data_from_core(cmd4),.group_4_strobe_from_core(cmd4_strobe),.group_4_oe_from_core(4'hf),.group_4_strobe_out_en(4'hf),
.group_4_data_out({par, ck_n, ck_p, cke, act_n, cs_n, dram_reset_n, bg[1]}),.group_4_strobe_out(odt),
.group_5_data_from_core(cmd5),.group_5_strobe_from_core(cmd5_strobe),.group_5_oe_from_core(4'hf),.group_5_strobe_out_en(4'hf),
.group_5_data_out({bg[0], ba[1], ba[0], a[16], a[15], a[14], a[13], a[12], a[11], a[10], a[9], a[8], a[7], a[6], a[3], a[2], a[1], a[0]}),.group_5_strobe_out(a[4]));
endmodule
