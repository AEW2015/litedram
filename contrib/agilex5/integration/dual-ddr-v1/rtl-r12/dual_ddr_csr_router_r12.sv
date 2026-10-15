`timescale 1ns/1ps
// One outstanding host CSR transaction; destination and command remain held
// through its response. Bank DMA engines are independent of this selector.
// Reset contract: all mailbox endpoints must reset together, even when idle.
// The physical top asserts coordinated mailbox reset if either PHY loses lock;
// independent destination-only reset is forbidden because it replays toggles.
module dual_ddr_csr_router_r12(
 input logic clk,rst,mailbox_rst,input logic [11:0] av_address,
 input logic av_read,av_write,input logic [31:0] av_writedata,
 output logic av_waitrequest,av_readdatavalid,output logic [31:0] av_readdata,
 input logic [1:0] bank_clk,bank_rst,bank_locked,owner_active,
 output logic bank_select,output logic busy,output logic fault,
 output logic [1:0][11:0] b_address,output logic [1:0] b_read,b_write,
 output logic [1:0][31:0] b_writedata,
 input logic [1:0] b_waitrequest,b_readdatavalid,input logic [1:0][31:0] b_readdata
);
 logic [1:0] req,ack;
 (* ASYNC_REG="TRUE", preserve, dont_merge, no_retiming,
 altera_attribute="-name SYNCHRONIZER_IDENTIFICATION FORCED_IF_ASYNCHRONOUS" *) logic [1:0] ak1,ak2,ak3,lock1,lock2,lock3;
 (* preserve, dont_merge, no_retiming *) logic [1:0] ack_capture,locked_capture;
 logic [1:0][11:0] address_hold;
 logic [1:0][31:0] data_hold,response_hold;
 logic [1:0] write_hold;
 logic selected_hold;
 logic [31:0] result,metadata_result;logic response_valid,metadata_valid;
 wire select_csr=av_address==12'h7f0;
 wire lock_csr=av_address==12'h7f4;
 wire metadata=select_csr || lock_csr;
 assign busy=pending;
 logic pending,issue_pending;
 (* preserve,dont_merge,no_retiming *) logic [11:0] command_address_capture;
 (* preserve,dont_merge,no_retiming *) logic [31:0] command_data_capture;
 (* preserve,dont_merge,no_retiming *) logic command_write_capture,command_bank_capture;
 // Unconditional ordinary input FFs keep the host bridge data path short.
 // Accepted tag is registered separately; next cycle uses the old captured
 // packet even if the master immediately changes a continuously valid bus.
 always_ff @(posedge clk or posedge rst)begin
  if(rst)begin command_address_capture<=0;command_data_capture<=0;command_write_capture<=0;command_bank_capture<=0;end
  else begin command_address_capture<=av_address;command_data_capture<=av_writedata;command_write_capture<=av_write;command_bank_capture<=bank_select;end
 end
 always_comb begin
  av_waitrequest=1;
  av_readdatavalid=response_valid|metadata_valid;av_readdata=metadata_valid?metadata_result:result;
  if(!pending && metadata)begin
   av_waitrequest=0;
  end else if(!pending && !metadata && locked_capture[bank_select] && !fault && !mailbox_rst)begin
   av_waitrequest=0;
  end
 end
 always_ff @(posedge clk or posedge rst)begin
  if(rst)begin
   lock1<=0;lock2<=0;lock3<=0;locked_capture<=0;
   bank_select<=0;fault<=0;metadata_valid<=0;metadata_result<=0;
  end else begin
   lock1<=bank_locked;lock2<=lock1;lock3<=lock2;locked_capture<=lock3;
   metadata_valid<=0;
   if(!pending && metadata && av_read && !av_waitrequest)begin
    metadata_result<=select_csr?{31'b0,bank_select}:{30'b0,locked_capture};metadata_valid<=1;
   end
   if(!pending && select_csr && av_write && !av_waitrequest)begin
    if(|av_writedata[31:1] || |owner_active)fault<=1;
    else bank_select<=av_writedata[0];
   end
  end
 end
 // Metadata/status stays readable while a PHY is unlocked. Mailbox reset is
 // separate and coordinated across both destinations; it cancels outstanding
 // bus work, so a reset during a CSR read also requires resetting the master.
 always_ff @(posedge clk or posedge rst or posedge mailbox_rst)begin
  if(rst || mailbox_rst)begin
   req<=0;ak1<=0;ak2<=0;ak3<=0;ack_capture<=0;pending<=0;issue_pending<=0;
   address_hold<=0;data_hold<=0;write_hold<=0;selected_hold<=0;result<=0;response_valid<=0;
  end else begin
   ak1<=ack;ak2<=ak1;ak3<=ak2;ack_capture<=ak3;
   response_valid<=0;
   if(!pending && !metadata && (av_read||av_write) && !av_waitrequest)begin
    pending<=1;issue_pending<=1;
   end
   if(issue_pending)begin
    selected_hold<=command_bank_capture;address_hold[command_bank_capture]<=command_address_capture;
    data_hold[command_bank_capture]<=command_data_capture;write_hold[command_bank_capture]<=command_write_capture;
    req[command_bank_capture]<=~req[command_bank_capture];issue_pending<=0;
   end
   if(pending && !issue_pending && ack_capture[selected_hold]==req[selected_hold])begin
    result<=response_hold[selected_hold];response_valid<=!write_hold[selected_hold];pending<=0;
   end
  end
 end
 for(genvar b=0;b<2;b++)begin:g_bank
  (* ASYNC_REG="TRUE", preserve, dont_merge, no_retiming,
  altera_attribute="-name SYNCHRONIZER_IDENTIFICATION FORCED_IF_ASYNCHRONOUS" *) logic rq1,rq2,rq3;
  (* preserve, dont_merge, no_retiming *) logic request_capture;
  logic seen,destination_write;logic [1:0] state;
  assign b_read[b]=(state==1)&&!destination_write;
  assign b_write[b]=(state==1)&&destination_write;
  always_ff @(posedge bank_clk[b] or posedge bank_rst[b])begin
   if(bank_rst[b])begin
    rq1<=0;rq2<=0;rq3<=0;request_capture<=0;seen<=0;ack[b]<=0;
    state<=0;destination_write<=0;b_address[b]<=0;b_writedata[b]<=0;response_hold[b]<=0;
   end else begin
    rq1<=req[b];rq2<=rq1;rq3<=rq2;request_capture<=rq3;
    case(state)
     0:if(request_capture!=seen)begin
      seen<=request_capture;destination_write<=write_hold[b];b_address[b]<=address_hold[b];b_writedata[b]<=data_hold[b];state<=1;
     end
     1:if(!b_waitrequest[b])begin
      if(destination_write)begin response_hold[b]<=0;ack[b]<=seen;state<=0;end
      else if(b_readdatavalid[b])begin response_hold[b]<=b_readdata[b];ack[b]<=seen;state<=0;end
      else state<=2;
     end
     2:if(b_readdatavalid[b])begin response_hold[b]<=b_readdata[b];ack[b]<=seen;state<=0;end
     default:state<=0;
    endcase
   end
  end
 end
endmodule
