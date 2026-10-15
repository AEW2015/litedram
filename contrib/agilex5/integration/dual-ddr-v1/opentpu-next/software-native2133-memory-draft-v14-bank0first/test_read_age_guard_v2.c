#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "native2133_read_age_guard_v2.h"
typedef struct {
    uint32_t before,after,capture,first,last,flags,valid;
    unsigned trace[16],trace_n;int issue_ok;
} mock;
static uint32_t read_csr(void*v,uint32_t csr){mock*m=v;m->trace[m->trace_n++]=csr;switch(csr){case 29:return m->trace_n==1?m->before:m->after;case 28:return m->capture;case 43:return m->first;case 44:return m->last;case 42:return m->flags;case 27:return m->valid;default:assert(!"unexpected CSR read");return 0;}}
static int issue_read(void*v,uint32_t address){mock*m=v;assert(address==0x123u);m->trace[m->trace_n++]=0xffffffffu;return m->issue_ok;}
static int run(mock*m,int payload_correct){native2133_read_age_observation o;int metadata_ok=native2133_checked_isolated_read(m,0x123u,read_csr,issue_read,&o);return metadata_ok&&payload_correct;}
static void trace_is_ordered(const mock*m){const unsigned expected[]={29,0xffffffffu,29,28,43,44,42,27};assert(m->trace_n==8);assert(memcmp(m->trace,expected,sizeof(expected))==0);}
static mock base(void){mock m={0};m.before=100;m.after=101;m.capture=m.valid=0xffff;m.first=m.last=16;m.issue_ok=1;return m;}
int main(void){
 mock m=base();assert(run(&m,1));trace_is_ordered(&m);
 m=base();m.after=102;m.first=17;m.last=18;assert(run(&m,1));trace_is_ordered(&m);
 m=base();m.first=18;m.last=18;assert(run(&m,1));
 m=base();m.before=0xffffffffu;m.after=0u;m.first=m.last=16;assert(run(&m,1)); /* modulo-32 counter wrap */
 m=base();m.after=102;m.first=18;m.last=19;assert(run(&m,1)); /* split return at latest modeled first age */
 m=base();m.first=15;m.last=15;assert(!run(&m,1)); /* correct data, too early */
 m=base();m.first=19;m.last=19;assert(!run(&m,1)); /* correct data, too late */
 m=base();m.after=102;m.first=16;m.last=18;assert(!run(&m,1)); /* age span too wide */
 m=base();m.after=100;assert(!run(&m,1)); /* stale ages */
 m=base();m.after=103;assert(!run(&m,1)); /* too many valid events */
 m=base();m.capture=0x7fff;assert(!run(&m,1));
 m=base();m.valid=0xfffe;assert(!run(&m,1));
 m=base();m.flags=1;assert(!run(&m,1));
 m=base();m.issue_ok=0;assert(!run(&m,1));
 m=base();assert(!run(&m,0));
 puts("PASS native2133 isolated-read age guard ordering/bounds/mask/flags/payload cases");return 0;
}
