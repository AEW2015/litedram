"""Reproducible post-generation32-reset/FIFO/V11 repairs, no frozen file edits."""
from pathlib import Path
import re,json,hashlib,argparse,textwrap
N=Path(__file__).resolve().parent;R=N.parents[2]
def local_resets(s):
 a=s.rindex('\tif (sys_rst) begin');start=a+len('\tif (sys_rst) begin\n');depth=1
 for m in re.finditer(r'\b(begin|end)\b',s[start:]):
  depth+=1 if m[0]=='begin' else -1
  if depth==0:z=start+m.start();break
 lines=[line for line in s[start:z].splitlines() if line.strip()];assert all(re.fullmatch(r'\s*\w+\s*<=\s*[^;]+;',line) for line in lines)
 # Small immutable policy replaces reading the historical generated RTL.
 policy=json.loads((R/'integration/dual-ddr-v1/legacy_reset_map.json').read_text())
 assert policy['schema']=='agilex5-legacy-local-reset-policy-v1'
 legacy={name:int(group) for name,group in policy['assignment_group'].items()}
 assert all(0<=group<32 for group in legacy.values())
 widths={m[2]:int(m[1])+1 if m[1] else 1 for m in re.finditer(r'\breg\s+(?:\[(\d+):0\]\s+)?(\w+)\s*(?:=|;)',s)};groups=[[] for _ in range(32)];weights=[0]*32;mapping={}
 for line in lines:
  name=re.match(r'\s*(\w+)',line)[1];group=legacy.get(name,min(range(32),key=lambda i:weights[i]));groups[group].append(line);weights[group]+=widths.get(name,1);mapping[name]=group
 s=s[:a]+'\n'.join('\tif (local_reset[%d]) begin\n%s\n\tend'%(g,'\n'.join(values)) for g,values in enumerate(groups))+s[z+3:]
 needle='\tinput sys_rst\n);';assert needle in s;s=s.replace(needle,needle+'''
(* preserve, dont_merge, no_retiming *) reg [31:0] local_reset=32'hffffffff;
always @(posedge sys_clk or posedge sys_rst)begin
 if(sys_rst)local_reset<=32'hffffffff;else local_reset<=0;
end
''',1)
 return s,{'assignment_group':mapping,'weights':weights,'release_cycles':1,'same_existing_reset_policy':True,'legacy_register_mappings_retained':sum(name in legacy for name in mapping),'new_register_mappings':{name:group for name,group in mapping.items() if name not in legacy},'legacy_V11_RTL_sha256':policy['donor_rtl_sha256']}
def first_error_and_fifo(s):
 marker='(* preserve, dont_merge, no_retiming *) reg [31:0] local_reset';assert s.count(marker)==1
 s=s.replace(marker,'''reg dma_first_error_seen=0;
always @(posedge sys_clk)begin
 if(local_reset[0])dma_first_error_seen<=0;
 else if(dma_start && !dma_busy)dma_first_error_seen<=0;
 else if(dma_busy && dma_verify_valid && (dma_mismatch_lanes!=0))dma_first_error_seen<=1;
end
'''+marker)
 old="if ((dma_errors_counter_immediateeventcounter_value == 1'd0)) begin";assert s.count(old)==1;s=s.replace(old,'if (!dma_first_error_seen) begin')
 old='(* ramstyle = "MLAB, no_rw_check" *) reg [257:0] storage_32[0:63];';assert s.count(old)==1;s=s.replace(old,old.replace('MLAB','M20K'))
 return s
def v11_layers(s):
 # Reuse the exact sealed transformation logic as a pure function. Strip its
 # path selection and final file I/O; execute no frozen script/module body.
 p=R/'integration/dual-ddr-v1/controller-fast-v11/prepare.py';text=p.read_text();a=text.index('# Dedicated same-edge router');b=text.index("(N/'litedram_external_controller.v').write_text(s)",a);body=text[a:b].rstrip().rstrip(';')
 assert '.write_text' not in body and '.read_text' not in body and 'N/' not in body
 function='def apply(s):\n old=s\n'+textwrap.indent(body,' ')+'\n return s,{"CSR_mux_terms":len(terms),"CSR_partitions":len(partitions),"CSR_max_partition_terms":max(map(len,partitions.values())),"read_return_extra_cycles":1,"first_bad_start_reset":reset_start,"first_bad_reset":reset_bad,"read_valid_reset":reset_valid}\n'
 ns={'re':re};exec(compile(function,str(p)+'[pure-layer]','exec'),ns);out,record=ns['apply'](s);record['sealed_layer_source_sha256']=hashlib.sha256(p.read_bytes()).hexdigest();return out,record
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--directory',required=True);a=p.parse_args();d=Path(a.directory);raw=d/'litedram_external_controller_raw.v';out=d/'litedram_external_controller.v';assert not out.exists()
 s,reset=local_resets(raw.read_text());s=first_error_and_fifo(s);s,repair=v11_layers(s);out.write_text(s)
 (d/'repair_layers.json').write_text(json.dumps({'prototype_only':True,'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'layered_sha256':hashlib.sha256(s.encode()).hexdigest(),'reset':reset,'V11':repair,'M20K_FIFO':'storage_32 64x258 unchanged sync ports','DFI_command_payload_scheduling_unchanged':True},indent=2)+'\n');print(out)
