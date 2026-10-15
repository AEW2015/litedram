from pathlib import Path
import re,json
N=Path(__file__).resolve().parent;R=N.parents[2];s=(N/'generated-r4/litedram_external_controller.v').read_text();(N/'gold-r4.v').write_text(s.replace('module litedram_external_controller(', 'module golden_controller(',1))
header=s[:s.index(');')];inputs=[]
for m in re.finditer(r'\binput\s*(\[\d+:0\]\s*)?(\w+)',header):inputs.append((m[1] or '',m[2]))
ports=','.join('.%s(%s)'%(name,'clk' if name=='sys_clk' else 'rst' if name=='sys_rst' else name) for _,name in inputs)
goldports=ports.replace('.sys_rst(rst)', '.sys_rst(dut.local_reset[0])')
mapping=json.loads((N/'generated-r4/reset_distribution.json').read_text())['assignment_group']
tb='''`timescale 1ns/1ps
module tb_reset_distribution;
bit clk=0,rst=1;always #2.5 clk=~clk;
'''+''.join('logic %s%s=0;\n'%(width,name) for width,name in inputs if name not in ['sys_clk','sys_rst'])+f'litedram_external_controller dut({ports});\ngolden_controller gold({goldports});\n'+'''integer cycles=0,reset_edges=0,grant_edges=0;
bit old_grant=0;
always @(posedge clk)begin
 #0.01;
 if(dut.local_reset!=={32{rst}})$fatal(1,"local reset replicas not identical source capture");
 cycles++;
 if(rst)reset_edges++;
 if(dut.c_granted && !old_grant)grant_edges++;
 old_grant=dut.c_granted;
'''+''.join('if(dut.%s!==gold.%s)$fatal(1,"state divergence %s cycle%%0d",cycles);\n'%(x,x,x) for x in mapping)+'''end
 task automatic csr(input logic[11:0] address,input logic[31:0] data);
 @(negedge clk);csr_address=address;csr_writedata=data;csr_write=1;
 do @(posedge clk);while(dut.csr_waitrequest);
 @(negedge clk);csr_write=0;
 repeat(20)@(negedge clk);
 endtask
 initial begin
 locked=1;c_rdata_ready=1;
 repeat(8)@(negedge clk);#0.45;rst=0;
 repeat(100)@(negedge clk);
 csr(4,8);c_request=1;repeat(250)@(negedge clk);
 if(!dut.c_granted)$fatal(1,"ownership grant missing");
 c_request=0;repeat(100)@(negedge clk);
 // Assert/deassert at independent phases, including near native edges.
 #0.05;rst=1;repeat(5)@(negedge clk);#0.45;rst=0;
 repeat(100)@(negedge clk);
 #2.45;rst=1;repeat(5)@(negedge clk);#2.55;rst=0;
 repeat(100)@(negedge clk);
 csr(4,8);c_request=1;repeat(250)@(negedge clk);
 if(!dut.c_granted)$fatal(1,"ownership regrant missing");
 if(reset_edges<10 || grant_edges<2)$fatal(1,"reset/grant coverage missing");
 $display("PASS controller reset distribution cycles=%0d reset_edges=%0d grant_edges=%0d compared_fields=1532 replicas=32",cycles,reset_edges,grant_edges);
 $finish;
 end
 initial begin #100000;$fatal(1,"timeout");end
endmodule
''';(N/'tb_reset_distribution-r4.sv').write_text(tb)
