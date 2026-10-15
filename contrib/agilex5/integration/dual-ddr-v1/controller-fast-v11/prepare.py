from pathlib import Path
import re,json,hashlib
R=Path(__file__).resolve().parents[3];N=Path(__file__).resolve().parent;A=R/'build/dual_ddr_opentpu_v10/litedram_external_controller.v';s=A.read_text();old=s
# Dedicated same-edge router address copy supplies only five configuration decoders.
s=s.replace('\tinput [11:0] csr_address,','\tinput [11:0] csr_address,\n\tinput [11:0] csr_config_address,');assert s!=old
for n in [25,26,40,41,78]:
 pattern=rf'(csr_request_pipeline_phy_config_write_{n} <= \(\(~csr_read\) & \()csr_address(\[11:2\] == [^\n]+)'
 s,count=re.subn(pattern,r'\1csr_config_address\2',s);assert count==1,(n,count)
# Same-edge WE-only one-hot copies break fanout from address-selection snapshots.
selector='dma_make_core_multiplexer_steerer_steerer_bank_address_select_snapshot';decl=f'(* preserve_syn_only = "true" *) reg [15:0] {selector} = 16\'d0;';assert decl in s
copies=[f'dfi_we_select_phase{i}' for i in range(4)];s=s.replace(decl,decl+'\n'+'\n'.join(f'(* preserve, dont_merge, no_retiming *) reg [15:0] {name}=0;' for name in copies))
for bit in range(16):
 pattern=rf'^(\s*{selector}\[{bit}\] <= ([^\n]+))$';match=re.search(pattern,s,re.M);assert match
 s=s.replace(match.group(1),match.group(1)+'\n'+'\n'.join(f'\t{name}[{bit}] <= {match.group(2)}' for name in copies))
reset=f'\t\t{selector} <= 16\'d0;';assert s.count(reset)==1;s=s.replace(reset,reset+'\n'+'\n'.join(f'\t\t{name}<=0;' for name in copies))
for phase,name in enumerate(copies):
 pattern=rf'^assign dma_make_core_dfi_p{phase}_we_n = [^\n]+';match=re.search(pattern,s,re.M);assert match and selector in match[0];s=s.replace(match[0],match[0].replace(selector,name))
# Four same-edge ordinary start copies each clear seven first_bad bits.
m=re.search(r'^\s*dma_start <= ([^\n]+);$',s,re.M);assert m;rhs=m[1]
mstart=re.search(r'if \(local_reset\[(\d+)\]\) begin\n(?:(?!\n\tend).)*?dma_start <= 1\'d0;',s,re.S);assert mstart;reset_start=int(mstart[1])
mbad=re.search(r'if \(local_reset\[(\d+)\]\) begin\n(?:(?!\n\tend).)*?dma_first_bad <= 28\'d0;',s,re.S);assert mbad;reset_bad=int(mbad[1])
for text in ["\t\tdma_first_bad <= 1'd0;","\t\t\t\t\tdma_first_bad <= dma_verify_addr;","\t\tdma_first_bad <= 28'd0;"]:
 assert s.count(text)==1,(text,s.count(text));s=s.replace(text,'')
extra='\n// V11 local same-edge start replicas; no command/data latency change.\n'
for g in range(4):
 extra+=f'''(* preserve, dont_merge, no_retiming *) reg first_bad_start_local{g}=0;
always @(posedge sys_clk) begin
 if(local_reset[{reset_start}])first_bad_start_local{g}<=0;
 else first_bad_start_local{g}<={rhs};
 if(local_reset[{reset_bad}])dma_first_bad[{g*7}+:7]<=0;
 else if(first_bad_start_local{g} && !dma_busy)dma_first_bad[{g*7}+:7]<=0;
 else if(dma_busy && dma_verify_valid && (dma_mismatch_lanes!=0) && !dma_first_error_seen)
  dma_first_bad[{g*7}+:7]<=dma_verify_addr[{g*7}+:7];
end
'''
# CSR return stage samples exactly the old read edge; only diagnostic response adds1cycle.
validold='\tinternal_csr_readdatavalid <= csr_write_data_pipeline_accepted_read;';assert s.count(validold)==1
sampleold='\tif (internal_csr_read) begin\n\t\tinternal_csr_readdata <= read_mux;\n\tend';assert s.count(sampleold)==1
mvalid=re.search(r'if \(local_reset\[(\d+)\]\) begin\n(?:(?!\n\tend).)*?internal_csr_readdatavalid <= 1\'d0;',s,re.S);assert mvalid;reset_valid=int(mvalid[1])
s=s.replace(validold,'\tinternal_csr_readdatavalid <= csr_read_return_valid;').replace(sampleold,'\tif (csr_read_return_valid) begin\n\t\tinternal_csr_readdata <= csr_read_return_data;\n\tend')
# Partition the 62-term read OR mux BEFORE registering, not merely rename its sink.
line=re.search(r'^assign csr_read_mux_data = ([^\n]+);$',s,re.M);assert line
terms=re.findall(r"\(csr_request_pipeline_address_select\[(\d+)\] \? (.*?) : 32'd0\)",line[1]);assert len(terms)==62==line[1].count("csr_request_pipeline_address_select["),len(terms)
assert len({int(index) for index,value in terms})==62
partitions={}
for index,value in terms:partitions.setdefault(int(index)//8,[]).append((int(index),value))
assert max(map(len,partitions.values()))<=8
extra+="wire [31:0] csr_read_return_data;\n"
groupnames=[]
for group,items in sorted(partitions.items()):
 name=f'csr_read_return_group{group}';groupnames.append(name)
 expression=" | ".join(f"(csr_request_pipeline_address_select[{index}] ? {value} : 32'd0)" for index,value in items)
 extra+=f"(* preserve, dont_merge, no_retiming *) reg [31:0] {name};\nalways @(posedge sys_clk) {name} <= {expression};\n"
extra+="assign csr_read_return_data="+" | ".join(groupnames)+";\n"
extra+=f"""(* preserve, dont_merge, no_retiming *) reg csr_read_return_valid=0;
always @(posedge sys_clk)begin
 if(local_reset[{reset_valid}])csr_read_return_valid<=0;
 else csr_read_return_valid<=csr_write_data_pipeline_accepted_read;
end
"""
s=s.replace('\nendmodule',extra+'\nendmodule');(N/'litedram_external_controller.v').write_text(s)
(N/'changes.json').write_text(json.dumps({'parent':str(A.relative_to(R)),'parent_sha256':hashlib.sha256(A.read_bytes()).hexdigest(),'RTL_sha256':hashlib.sha256(s.encode()).hexdigest(),'local_start_reset_index':reset_start,'first_bad_reset_index':reset_bad,'read_return_valid_reset_index':reset_valid,'WE_same_edge_copies':64,'first_bad_start_same_edge_copies':4,'CSR_read_mux_terms':len(terms),'CSR_read_partition_count':len(partitions),'CSR_read_max_terms_per_partition':max(map(len,partitions.values())),'CSR_read_response_additional_cycles':1,'DFI_and_native_pipeline_cycles_changed':0},indent=2)+'\n')
print(N,reset_start,reset_bad,reset_valid)
