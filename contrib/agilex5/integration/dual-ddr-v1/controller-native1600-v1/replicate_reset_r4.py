#!/usr/bin/env python3
"""Same proven32-local-reset policy applied to the freshly regenerated200MHz RTL."""
from pathlib import Path
import re,json,hashlib
N=Path(__file__).resolve().parent;A=N/'generated-r4/litedram_external_controller.v';B=N/'generated-r4/litedram_external_controller_local_reset.v';assert not B.exists();s=A.read_text();a=s.rindex('\tif (sys_rst) begin');start=a+len('\tif (sys_rst) begin\n');depth=1
for m in re.finditer(r'\b(begin|end)\b',s[start:]):
 depth+=1 if m[0]=='begin' else -1
 if depth==0:z=start+m.start();break
lines=[l for l in s[start:z].splitlines() if l.strip()];assert all(re.fullmatch(r'\s*\w+\s*<=\s*[^;]+;',l) for l in lines)
widths={m[2]:int(m[1])+1 if m[1] else 1 for m in re.finditer(r'\breg\s+(?:\[(\d+):0\]\s+)?(\w+)\s*(?:=|;)',s)};groups=[[] for _ in range(32)];weights=[0]*32;mapping={}
for line in lines:
 name=re.match(r'\s*(\w+)',line)[1];g=min(range(32),key=lambda j:weights[j]);groups[g].append(line);weights[g]+=widths.get(name,1);mapping[name]=g
s=s[:a]+'\n'.join('\tif (local_reset[%d]) begin\n%s\n\tend'%(g,'\n'.join(v)) for g,v in enumerate(groups))+s[z+3:]
needle='\tinput sys_rst\n);';assert needle in s;s=s.replace(needle,needle+'''
(* preserve, dont_merge, no_retiming *) reg [31:0] local_reset=32'hffffffff;
always @(posedge sys_clk or posedge sys_rst) begin
 if(sys_rst)local_reset<=32'hffffffff;else local_reset<=32'b0;
end
''',1);B.write_text(s);sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();(N/'generated-r4/reset_distribution.json').write_text(json.dumps({'parent':str(A),'parent_sha256':sha(A),'output_sha256':sha(B),'assignment_group':mapping,'weights':weights,'added_reset_release_cycles':1,'clock_hz':200000000,'data_latency_change':0},indent=2)+'\n');print(len(mapping),sum(weights),max(weights),sha(B))
