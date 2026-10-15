// Two independent physical IO96B calibration services. Public AXI address bit26
// selects the service and is stripped before the original 27-bit slave address.
// AW/W are captured independently; each response retains its accepted bank tag.
module dual_ddr_cal_service(
 output wire [1341:0] periph_calbus_0,periph_calbus_1,
 input wire [1314:0] periph_calbus_readdata_0,periph_calbus_readdata_1,
 input wire [32767:0] periph_calbus_param_table_0,periph_calbus_param_table_1,
 input logic s0_axi4lite_clk,s0_axi4lite_rst_n,
 input logic [26:0] s0_axi4lite_awaddr,s0_axi4lite_araddr,
 input logic s0_axi4lite_awvalid,s0_axi4lite_arvalid,s0_axi4lite_wvalid,
 output logic s0_axi4lite_awready,s0_axi4lite_arready,s0_axi4lite_wready,
 input logic [31:0] s0_axi4lite_wdata,input logic [3:0] s0_axi4lite_wstrb,
 input logic [2:0] s0_axi4lite_awprot,s0_axi4lite_arprot,
 output logic [31:0] s0_axi4lite_rdata,output logic [1:0] s0_axi4lite_rresp,s0_axi4lite_bresp,
 output logic s0_axi4lite_rvalid,s0_axi4lite_bvalid,
 input logic s0_axi4lite_rready,s0_axi4lite_bready
);
 logic aw_saved,w_saved,write_active,aw_sent,w_sent,write_bank;
 logic [25:0] aw_hold;logic [31:0] w_hold;logic [3:0] strobe_hold;logic [2:0] awprot_hold;
 logic read_active,ar_sent,read_bank;logic [25:0] ar_hold;logic [2:0] arprot_hold;
 wire [1:0] awready,wready,arready,bvalid,rvalid;
 wire [1:0][31:0] rdata;wire [1:0][1:0] bresp,rresp;
 assign s0_axi4lite_awready=!aw_saved&&!write_active;
 assign s0_axi4lite_wready=!w_saved&&!write_active;
 assign s0_axi4lite_arready=!read_active;
 assign s0_axi4lite_bvalid=write_active&&aw_sent&&w_sent&&bvalid[write_bank];
 assign s0_axi4lite_bresp=bresp[write_bank];
 assign s0_axi4lite_rvalid=read_active&&ar_sent&&rvalid[read_bank];
 assign s0_axi4lite_rdata=rdata[read_bank];assign s0_axi4lite_rresp=rresp[read_bank];
 always_ff @(posedge s0_axi4lite_clk or negedge s0_axi4lite_rst_n)begin
  if(!s0_axi4lite_rst_n)begin
   aw_saved<=0;w_saved<=0;write_active<=0;aw_sent<=0;w_sent<=0;
   read_active<=0;ar_sent<=0;write_bank<=0;read_bank<=0;
   aw_hold<=0;w_hold<=0;strobe_hold<=0;ar_hold<=0;awprot_hold<=0;arprot_hold<=0;
  end else begin
   if(s0_axi4lite_awvalid&&s0_axi4lite_awready)begin
    aw_saved<=1;write_bank<=s0_axi4lite_awaddr[26];aw_hold<=s0_axi4lite_awaddr[25:0];awprot_hold<=s0_axi4lite_awprot;
   end
   if(s0_axi4lite_wvalid&&s0_axi4lite_wready)begin
    w_saved<=1;w_hold<=s0_axi4lite_wdata;strobe_hold<=s0_axi4lite_wstrb;
   end
   if(aw_saved&&w_saved&&!write_active)begin write_active<=1;aw_sent<=0;w_sent<=0;end
   if(write_active&&!aw_sent&&awready[write_bank])aw_sent<=1;
   if(write_active&&!w_sent&&wready[write_bank])w_sent<=1;
   if(s0_axi4lite_bvalid&&s0_axi4lite_bready)begin write_active<=0;aw_saved<=0;w_saved<=0;end
   if(s0_axi4lite_arvalid&&s0_axi4lite_arready)begin
    read_active<=1;ar_sent<=0;read_bank<=s0_axi4lite_araddr[26];ar_hold<=s0_axi4lite_araddr[25:0];arprot_hold<=s0_axi4lite_arprot;
   end
   if(read_active&&!ar_sent&&arready[read_bank])ar_sent<=1;
   if(s0_axi4lite_rvalid&&s0_axi4lite_rready)read_active<=0;
  end
 end
 for(genvar b=0;b<2;b++)begin:g_hub
  wire [1341:0] calbus;wire [1314:0] calread;wire [32767:0] caltable;
  if(b==0)begin
   assign periph_calbus_0=calbus;assign calread=periph_calbus_readdata_0;assign caltable=periph_calbus_param_table_0;
  end else begin
   assign periph_calbus_1=calbus;assign calread=periph_calbus_readdata_1;assign caltable=periph_calbus_param_table_1;
  end
  control_system_cal_2 u_hub(
   .periph_calbus_0(calbus),.periph_calbus_readdata_0(calread),.periph_calbus_param_table_0(caltable),
   .s0_axi4lite_clk,.s0_axi4lite_rst_n,
   .s0_axi4lite_awaddr({1'b0,aw_hold}),.s0_axi4lite_awprot(awprot_hold),
   .s0_axi4lite_awvalid(write_active&&!aw_sent&&write_bank==b),.s0_axi4lite_awready(awready[b]),
   .s0_axi4lite_wdata(w_hold),.s0_axi4lite_wstrb(strobe_hold),
   .s0_axi4lite_wvalid(write_active&&!w_sent&&write_bank==b),.s0_axi4lite_wready(wready[b]),
   .s0_axi4lite_bvalid(bvalid[b]),.s0_axi4lite_bresp(bresp[b]),
   .s0_axi4lite_bready(write_active&&aw_sent&&w_sent&&write_bank==b&&s0_axi4lite_bready),
   .s0_axi4lite_araddr({1'b0,ar_hold}),.s0_axi4lite_arprot(arprot_hold),
   .s0_axi4lite_arvalid(read_active&&!ar_sent&&read_bank==b),.s0_axi4lite_arready(arready[b]),
   .s0_axi4lite_rvalid(rvalid[b]),.s0_axi4lite_rdata(rdata[b]),.s0_axi4lite_rresp(rresp[b]),
   .s0_axi4lite_rready(read_active&&ar_sent&&read_bank==b&&s0_axi4lite_rready));
 end
endmodule
