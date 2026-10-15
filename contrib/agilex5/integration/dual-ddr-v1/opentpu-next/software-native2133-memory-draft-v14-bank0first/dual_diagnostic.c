/* Fullrate CK/4 controller firmware probe for the isolated A5 variants. */
#define log_line fullrate_legacy_log_line
#define initialize public_litedram_legacy_initialize
#define main memory_test_main_disabled
#include "../../../../software/main.c"
#undef main
#undef initialize
#undef log_line

/* Generated from the audited PHY mapping; no inferred lane order. */
static unsigned dual_selected_bank;
static const unsigned dual_cal_bases[2]={0x08600000u,0x0c600000u};
static const unsigned dual_lane_map[2][4]={{4,0,5,6},{0,4,5,6}};
static const unsigned dual_ck_lanes[2]={1,1};
static const unsigned dual_ck_offsets[2][2]={{0xe0u,0xdcu},{0xe0u,0xdcu}};
static unsigned dual_cal_base(void){return dual_cal_bases[dual_selected_bank];}
static unsigned dual_ck_base(void){return dual_cal_base()+dual_ck_lanes[dual_selected_bank]*0x2000u;}
static void dual_data_lanes(unsigned lanes[4]){for(unsigned i=0;i<4;i++)lanes[i]=dual_lane_map[dual_selected_bank][i];}

/* Preserve complete CAS/queue diagnostics for multi-million-word transfers. */
static void log_line(const char *fmt, ...) {
 char line[512]; va_list ap; va_start(ap,fmt);
 int prefix=snprintf(line,sizeof(line),"BANK=%u ",dual_selected_bank);
 int body=vsnprintf(line+prefix,sizeof(line)-prefix,fmt,ap); va_end(ap);
 int n=body<0?-1:prefix+body;
 if(n<0 || n>=(int)sizeof(line)) return;
 uint64_t started=alt_niosv_mtime_get();
 for(int i=0;i<n;i++) {
  while(!(IORD_ALTERA_AVALON_JTAG_UART_CONTROL(JTAG_UART_BASE)>>16)) {
   if(alt_niosv_mtime_get()-started > ALT_CPU_CPU_FREQ/4u) return;
  }
  IOWR_ALTERA_AVALON_JTAG_UART_DATA(JTAG_UART_BASE,line[i]);
 }
}

#include "public_phy_calibration_dual.h"
#include "native2133_read_age_guard_v2.h"

#define DMA_CONTROL 64u
#define DMA_BASE 65u
#define DMA_LENGTH 66u
#define DMA_MODE 67u
#define DMA_SEED 68u
#define DMA_GAP 69u
#define DMA_STATUS 64u
#define DMA_CYCLES_LO 70u
#define DMA_CYCLES_HI 71u
#define DMA_ISSUED 72u
#define DMA_TRANSFERRED 73u
#define DMA_ERRORS 74u
#define DMA_FIRST_BAD_ADDR 75u
#define DMA_FIRST_BAD_OBS 76u
#define DMA_FIRST_BAD_EXPECTED 77u
#define DMA_DIAG_READ_CAS 79u
#define DMA_DIAG_WRITE_CAS 80u
#define DMA_DIAG_BANK_MANAGEMENT 81u
#define DMA_DIAG_REFRESH_CYCLES 82u
#define DMA_DIAG_STRIPED_ERRORS 83u
#define DMA_DIAG_STRIPED_DRAINED 84u
#define PUBLIC_GPIO_A5_DELAY 41u
#define PUBLIC_CMD_FLAGS     42u
#define PUBLIC_READ_MASK     40u
#define PUBLIC_MAX_ERRORS    4u
#ifndef PUBLIC_MEMORY_CLOCK_MHZ
#define PUBLIC_MEMORY_CLOCK_MHZ 1200u
#endif

static void primitive_set_ck_shift(unsigned shift);
static void fail_forever(const char *marker);
#ifndef PRIMITIVE_CK_SHIFT
#define PRIMITIVE_CK_SHIFT 0u
#endif
#ifndef PRIMITIVE_RX_RCVEN_CODE
#if PUBLIC_MEMORY_CLOCK_MHZ == 1067u
#define PRIMITIVE_RX_RCVEN_CODE 16u /* untrained starting value */
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1467u || PUBLIC_MEMORY_CLOCK_MHZ == 1333u || PUBLIC_MEMORY_CLOCK_MHZ == 1600u
#define PRIMITIVE_RX_RCVEN_CODE 128u
#else
#define PRIMITIVE_RX_RCVEN_CODE 64u
#endif
#endif
#if PUBLIC_MEMORY_CLOCK_MHZ == 1067u
#define PUBLIC_DDR_RATE "2133.333333"
#define PUBLIC_RX_RCVEN_CODE PRIMITIVE_RX_RCVEN_CODE
#define PUBLIC_RX_COLD_GATE 4u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 800u
#define PUBLIC_DDR_RATE "1600"
#define PUBLIC_RX_RCVEN_CODE PRIMITIVE_RX_RCVEN_CODE
#define PUBLIC_RX_COLD_GATE 3u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1600u
#define PUBLIC_DDR_RATE "3200"
#define PUBLIC_RX_RCVEN_CODE PRIMITIVE_RX_RCVEN_CODE
#define PUBLIC_RX_COLD_GATE 5u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1467u
#define PUBLIC_DDR_RATE "2933.333333"
#define PUBLIC_RX_RCVEN_CODE PRIMITIVE_RX_RCVEN_CODE
#define PUBLIC_RX_COLD_GATE 5u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1333u
#define PUBLIC_DDR_RATE "2666.666667"
#define PUBLIC_RX_RCVEN_CODE PRIMITIVE_RX_RCVEN_CODE
#define PUBLIC_RX_COLD_GATE 5u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1250u
#define PUBLIC_DDR_RATE "2500"
#define PUBLIC_RX_RCVEN_CODE 64u
#define PUBLIC_RX_COLD_GATE 5u
#else
#define PUBLIC_DDR_RATE "2400"
#define PUBLIC_RX_RCVEN_CODE 64u
#define PUBLIC_RX_COLD_GATE 4u
#endif

#if PUBLIC_MEMORY_CLOCK_MHZ == 1067u
#define PUBLIC_DDR_CL 16u
#define PUBLIC_DDR_CWL 11u
#define PUBLIC_DDR_WR 20u
#define PUBLIC_DDR_RTP 10u
#define PUBLIC_DDR_TCCD 6u
#define PUBLIC_DDR_MR0 0x0b34u
#define PUBLIC_DDR_MR2 0x210u
#define PUBLIC_COMMAND_PHASE 3u
#define PUBLIC_FAST_READ_PHASE 3u
#define PUBLIC_FAST_WRITE_PHASE 1u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 800u
#define PUBLIC_DDR_CL 12u
#define PUBLIC_DDR_CWL 11u
#define PUBLIC_DDR_WR 12u
#define PUBLIC_DDR_RTP 6u
#define PUBLIC_DDR_TCCD 5u
#define PUBLIC_DDR_MR0 0x314u
#define PUBLIC_DDR_MR2 0x210u
#define PUBLIC_COMMAND_PHASE 3u
#define PUBLIC_FAST_READ_PHASE 3u
#define PUBLIC_FAST_WRITE_PHASE 1u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1600u
#define PUBLIC_DDR_CL 22u
#define PUBLIC_DDR_CWL 16u
#define PUBLIC_DDR_WR 24u
#define PUBLIC_DDR_RTP 12u
#define PUBLIC_DDR_TCCD 8u
#define PUBLIC_DDR_MR0 0x0d50u
#define PUBLIC_DDR_MR2 0x228u
#define PUBLIC_COMMAND_PHASE 1u
#define PUBLIC_FAST_READ_PHASE 1u
#define PUBLIC_FAST_WRITE_PHASE 0u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1467u
#define PUBLIC_DDR_CL 22u
#define PUBLIC_DDR_CWL 16u
#define PUBLIC_DDR_WR 24u
#define PUBLIC_DDR_RTP 12u
#define PUBLIC_DDR_TCCD 8u
#define PUBLIC_DDR_MR0 0x0d50u
#define PUBLIC_DDR_MR2 0x228u
/* CL22 + phase1 preserves the CL20 + phase3 burst position. */
#define PUBLIC_COMMAND_PHASE 1u
#define PUBLIC_FAST_READ_PHASE 1u
#define PUBLIC_FAST_WRITE_PHASE 0u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1333u
#define PUBLIC_DDR_CL 20u
#define PUBLIC_DDR_CWL 14u
#define PUBLIC_DDR_WR 20u
#define PUBLIC_DDR_RTP 10u
#define PUBLIC_DDR_TCCD 8u
#define PUBLIC_DDR_MR0 0x0b44u
#define PUBLIC_DDR_MR2 0x220u
#define PUBLIC_COMMAND_PHASE 3u
#define PUBLIC_FAST_READ_PHASE 3u
#define PUBLIC_FAST_WRITE_PHASE 2u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1250u
#define PUBLIC_DDR_CL 20u
#define PUBLIC_DDR_CWL 14u
#define PUBLIC_DDR_WR 20u
#define PUBLIC_DDR_RTP 10u
#define PUBLIC_DDR_TCCD 8u
#define PUBLIC_DDR_MR0 0x0b44u
#define PUBLIC_DDR_MR2 0x220u
#define PUBLIC_COMMAND_PHASE 3u
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1200u
#define PUBLIC_DDR_CL 18u
#define PUBLIC_DDR_CWL 12u
#define PUBLIC_DDR_WR 20u
#define PUBLIC_DDR_RTP 10u
#define PUBLIC_DDR_TCCD 8u
#define PUBLIC_DDR_MR0 0x0b40u
#define PUBLIC_DDR_MR2 0x218u
#define PUBLIC_COMMAND_PHASE 1u
#else
#error "This draft accepts only PUBLIC_MEMORY_CLOCK_MHZ=1067"
#endif

/* Keep the proven reset/MR sequence while selecting the latency profile that
 * matches this image's reference clock. MR0 selects CL; MR2 selects CWL. */
static void initialize_public_rate(void)
{
    wr(1, 0); delay_us(1000);
    primitive_set_ck_shift(PRIMITIVE_CK_SHIFT);
    wr(1, 1); delay_us(1000);
    wr(1, 3); delay_us(100);
    mode(3, 0);
    mode(6, ((PUBLIC_DDR_TCCD - 4u) << 10) | 0xe3u); delay_us(5);
    mode(6, ((PUBLIC_DDR_TCCD - 4u) << 10) | 0x63u); delay_us(5);
    mode(5, 0x100); /* RTT park240; DM/DBI and CA parity disabled. */
    mode(4, 0);
    mode(2, PUBLIC_DDR_MR2);
    mode(1, 0x301);
    mode(0, PUBLIC_DDR_MR0);
    delay_us(10);
    command(0x400, 0, 3);
    delay_us(10);
    log_line("DDR4_INIT_SENT CL%u CWL%u WR%u RTP%u manual_phase=%u DM_OFF\n",
        PUBLIC_DDR_CL, PUBLIC_DDR_CWL, PUBLIC_DDR_WR, PUBLIC_DDR_RTP,
        PUBLIC_COMMAND_PHASE);
    log_line("DDR4_TCCD_L=%uCK MR6=0x%03x/0x%03x\n",
        PUBLIC_DDR_TCCD, ((PUBLIC_DDR_TCCD - 4u) << 10) | 0xe3u,
        ((PUBLIC_DDR_TCCD - 4u) << 10) | 0x63u);
}

static uint32_t native2133_age_read_csr(void *context,uint32_t csr)
{ (void)context; return rd(csr); }
static int native2133_age_issue_read(void *context,uint32_t address)
{ (void)context; return public_controller_operation(address,0); }

static int operation_checked(unsigned address, int writing)
{
    if (writing) {
        if (!public_controller_operation(address, 1)) return 0;
        return rd(PUBLIC_CMD_FLAGS) == 0;
    }
    native2133_read_age_observation o;
    int ok=native2133_checked_isolated_read(0,address,native2133_age_read_csr,
                                             native2133_age_issue_read,&o);
    if (!ok && o.issued) {
        log_line("NATIVE2133_READ_AGE_REJECT address=%08lx count_before=%lu count_after=%lu delta=%lu first=%lu last=%lu capture=%04lx valid=%04lx flags=%08lx phase=%lu gate=%lu a5=%lu alignment=%lu\n",
            (unsigned long)address,(unsigned long)o.count_before,(unsigned long)o.count_after,
            (unsigned long)o.count_delta,(unsigned long)o.first_age,(unsigned long)o.last_age,
            (unsigned long)o.capture_mask,(unsigned long)o.valid_mask,(unsigned long)o.flags,
            (unsigned long)rd(24),(unsigned long)rd(25),(unsigned long)rd(41),(unsigned long)rd(78));
    }
    return ok;
}

/* Apply a small CK output phase while DRAM RESET_n is asserted and traffic is
 * idle. CK_p/n are byte-1 pins 8/9 (lane base 0x08602000). Preserve unrelated
 * timing-control fields and restore the original clock-gating register. */
static void primitive_set_ck_shift(unsigned shift)
{
    const unsigned base = dual_ck_base();
    const unsigned *offsets = dual_ck_offsets[dual_selected_bank];
    uint32_t clocks = IORD_32DIRECT(base, 0x104);
    uint32_t training = IORD_32DIRECT(base, 0x160) & ~(1u << 14);
    IOWR_32DIRECT(base, 0x104, clocks | (1u << 11));
    fence(); delay_us(10);
    for (unsigned step = 0; step < 3; ++step) {
        IOWR_32DIRECT(base, 0x160, training | ((step == 1) ? (1u << 14) : 0));
        fence(); delay_us(10);
    }
    for (unsigned pin = 0; pin < 2; ++pin) {
        uint32_t value = IORD_32DIRECT(base, offsets[pin]);
        value = (value & 0x001fffffu) | ((shift & 0x7ffu) << 21);
        IOWR_32DIRECT(base, offsets[pin], value);
    }
    fence(); delay_us(20);
    for (unsigned pin = 0; pin < 2; ++pin) {
        unsigned code = (IORD_32DIRECT(base, offsets[pin]) >> 21) & 0x7ffu;
        if (code != shift) fail_forever("PRIMITIVE_CK_SHIFT_READBACK_FAIL");
    }
    IOWR_32DIRECT(base, 0x104, clocks);
    fence(); delay_us(10);
    log_line("PRIM_CK_SHIFT code=%u pin8=%u pin9=%u\n", shift, shift, shift);
}

static unsigned check_mpr_pattern(uint32_t value, unsigned *valid_out)
{
    unsigned errors = 0;
    command(value ? 255u : 0u, 0, 7); /* MPR page 0 WRITE selects 0/FF pattern. */
    delay_us(10);
    command(0, 0, 37);                /* READ MPR0. */
    delay_us(2);
    unsigned valid = rd(28);
    if (valid_out) *valid_out = valid;
    errors += valid != 0xffffu;
    for (unsigned i = 0; i < 8; ++i) {
        uint32_t actual=rd(32+i);
        errors += actual!=value;
        if(actual!=value) log_line("MPR_OBSERVATION expected=%08lx i=%u actual=%08lx valid=%04x phase=%lu gate=%lu a5=%lu\n",
            (unsigned long)value,i,(unsigned long)actual,valid,
            (unsigned long)rd(24),(unsigned long)rd(25),(unsigned long)rd(41));
    }
    return errors;
}

static uint32_t training_word(uint32_t address, unsigned beat, unsigned pass)
{
    uint32_t x = address * 0x9e3779b9u + beat * 0x7f4a7c15u;
    x ^= pass ? 0xa5c39e71u : 0x6d2b79f5u;
    x ^= x >> 16; x *= 0x7feb352du;
    x ^= x >> 15; x *= 0x846ca68bu;
    x ^= x >> 16;
    return pass ? ~x : x;
}

static void fail_forever(const char *marker)
{
    log_line("%s\n", marker);
    for (;;) {
        IOWR_ALTERA_AVALON_PIO_DATA(LEDS_BASE, 1);
        delay_us(100000);
        IOWR_ALTERA_AVALON_PIO_DATA(LEDS_BASE, 0);
        delay_us(100000);
    }
}

/* Byte1 (physical lane0) has a measured write-DQS window104..120.
 * Center at112; other byte lanes retain DQ64/DQS96. Traffic is idle. */
static void primitive_set_training_tx(void)
{
    public_phy_set_tx_shift(64);
    if(dual_selected_bank) return;
    unsigned lanes[4]; dual_data_lanes(lanes);
    const unsigned offsets[2]={0xf0,0xec};
    uint32_t clocks[4],training[4];
    for(unsigned b=0;b<4;b++) {
        unsigned base=dual_cal_base()+lanes[b]*0x2000u;
        clocks[b]=IORD_32DIRECT(base,0x104);
        training[b]=IORD_32DIRECT(base,0x160)&~(1u<<14);
        IOWR_32DIRECT(base,0x104,clocks[b]|(1u<<11));
    }
    fence();delay_us(10);
    for(unsigned step=0;step<3;step++) {
        for(unsigned b=0;b<4;b++)IOWR_32DIRECT(dual_cal_base()+lanes[b]*0x2000u,0x160,
            training[b]|((step==1)?(1u<<14):0));
        fence();delay_us(10);
    }
    for(unsigned p=0;p<2;p++) {
        uint32_t value=IORD_32DIRECT(dual_cal_base(),offsets[p]);
        IOWR_32DIRECT(dual_cal_base(),offsets[p],(value&0x001fffffu)|(112u<<21));
    }
    fence();delay_us(20);
    for(unsigned p=0;p<2;p++)
        if((IORD_32DIRECT(dual_cal_base(),offsets[p])>>21)!=112u)
            fail_forever("PRIMITIVE_BYTE1_TX_DQS_READBACK_FAIL");
    for(unsigned b=0;b<4;b++)IOWR_32DIRECT(dual_cal_base()+lanes[b]*0x2000u,0x104,clocks[b]);
    fence();delay_us(10);
    log_line("PRIMITIVE_BYTE1_TX_DQS code=112 pin4=112 pin5=112 RX_preserved=1\n");
}

/* Verify mode checks lane i against (base + word) XOR seed XOR
 * (0x9e3779b9 * (i + 1)), with 32-bit arithmetic. */
static unsigned dma_calibration_mode;
static unsigned dma_cpu_a5 = 2u;
static unsigned dma_selected_read_a5 = 2u;
static unsigned dma_selected_write_a5 = 2u;
static unsigned dma_selected_frame;
static int dma_transfer(unsigned base, unsigned words, unsigned seed,
                        unsigned gap, unsigned mode)
{
    if (!dma_calibration_mode) {
        wr(41, mode ? dma_selected_read_a5 : dma_selected_write_a5);
        if (mode) {
            if(dma_selected_frame!=1u)fail_forever("NATIVE2133_ALIGNMENT1_DMA_FRAME_FAIL");
            wr(78,1u);
            if(rd(78)!=1u)fail_forever("NATIVE2133_ALIGNMENT1_DMA_READBACK_FAIL");
        }
        fence();
    }
    wr(DMA_CONTROL, 0);
    wr(DMA_BASE, base);
    wr(DMA_LENGTH, words);
    wr(DMA_MODE, mode);
    wr(DMA_SEED, seed);
    wr(DMA_GAP, gap);
    fence();
    wr(DMA_CONTROL, 1u);

    /* A hard 5 s bound keeps a wedged DMA from hiding the failure forever. */
    unsigned elapsed_ms = 0;
    unsigned status = 0;
    while (elapsed_ms < 5000u) {
        status = rd(DMA_STATUS);
        if (status & 2u) break;
        delay_us(1000);
        elapsed_ms++;
    }
    if (!(status & (2u | 4u))) {
        log_line("%s_FAIL mode=%s words=%u gap=%u timeout_ms=%u\n",
                 dma_calibration_mode?"DMA_CAL":"DMA",mode ? "read" : "write", words, gap, elapsed_ms);
        return 0;
    }

    uint32_t cycles_lo = rd(DMA_CYCLES_LO);
    uint32_t cycles_hi = rd(DMA_CYCLES_HI);
    unsigned errors = rd(DMA_ERRORS);
    unsigned transferred = rd(DMA_TRANSFERRED);
    unsigned issued = rd(DMA_ISSUED);
    unsigned read_cas = rd(DMA_DIAG_READ_CAS);
    unsigned write_cas = rd(DMA_DIAG_WRITE_CAS);
    unsigned bank_management = rd(DMA_DIAG_BANK_MANAGEMENT);
    unsigned refresh_cycles = rd(DMA_DIAG_REFRESH_CYCLES);
    unsigned striped_errors = rd(DMA_DIAG_STRIPED_ERRORS);
    unsigned striped_drained = rd(DMA_DIAG_STRIPED_DRAINED);
    unsigned command_conflict = rd(PUBLIC_CMD_FLAGS);
    unsigned hw_error = (status & 4u) != 0 || (status & 2u) == 0;
    log_line("DMA_DIAG read_cas=%u write_cas=%u bank_management=%u refresh_cycles=%u striped_errors=%u striped_drained=%u command_conflict=%u\n",
        read_cas, write_cas, bank_management, refresh_cycles,
        striped_errors, striped_drained, command_conflict);
    if (striped_errors != 0u || command_conflict != 0u || (mode && read_cas != words) ||
        (!mode && write_cas != words)) hw_error = 1u;
    log_line("%s_%s mode=%s words=%u gap=%u cycles=%08lx%08lx issued=%u moved=%u errors=%u\n",
        dma_calibration_mode?"DMA_CAL":"DMA",
        (hw_error || errors || transferred != words || issued != words) ? "FAIL" : "PASS",
        mode ? "R" : "W", words, gap, (unsigned long)cycles_hi, (unsigned long)cycles_lo,
        issued, transferred, errors);
    if (hw_error || errors || transferred != words || issued != words) {
        log_line("DMA_BAD addr=%08lx got=%08lx expected=%08lx\n",
            (unsigned long)rd(DMA_FIRST_BAD_ADDR),
            (unsigned long)rd(DMA_FIRST_BAD_OBS),
            (unsigned long)rd(DMA_FIRST_BAD_EXPECTED));
        return 0;
    }
    return 1;
}

static int dma_cpu_verify_at_tap(unsigned base, unsigned words, unsigned seed,
                                 unsigned gpio_tap)
{
    wr(41, gpio_tap);
    delay_us(100);
    unsigned errors=0;
    for (unsigned j=0; j<(words<=128?words:32u); ++j) {
        unsigned address=base+(words<=128?j:(j<16?j:words-32+j));
        if (!operation_checked(address,0)) return 0;
        for (unsigned lane=0;lane<8;++lane) {
            unsigned expected=address^seed^(0x9e3779b9u*(lane+1u));
            unsigned got=rd(16+lane);
            if (got!=expected) {
                if (errors<4) log_line("DMA_CPU_BAD a=%08lx lane=%u got=%08lx exp=%08lx\n",(unsigned long)address,lane,(unsigned long)got,(unsigned long)expected);
                ++errors;
            }
        }
    }
    log_line("DMA_CPU_VERIFY_TAP tap=%u\n",gpio_tap);
    log_line("DMA_CPU_VERIFY words=%u errors=%u\n",words,errors);
    return errors==0;
}
static int dma_cpu_verify(unsigned base, unsigned words, unsigned seed)
{
    return dma_cpu_verify_at_tap(base, words, seed, dma_cpu_a5);
}
static int dma_large_case(unsigned base, unsigned words, unsigned seed)
{
    log_line("DMA_DESCRIPTOR base=%08lx words=%u seed=%08lx\n",(unsigned long)base,words,(unsigned long)seed);
    if (!dma_transfer(base, words, seed, 0u, 0u)) return 0;
    if (!dma_cpu_verify(base, words, seed)) return 0;
    return dma_transfer(base, words, seed, 0u, 1u);
}

static int dma_copy_case(unsigned words, unsigned gap)
{
    const unsigned base = 0u;
    const unsigned seed = 0x6d2b79f5u;
    if (!dma_transfer(base, words, seed, gap, 0u)) return 0;
    if (!dma_cpu_verify(base,words,seed)) return 0;
    return dma_transfer(base, words, seed, gap, 1u);
}

#include "fullrate_gpio_calibration_native2133_draft_v2.h"
static int dma_ramp(void)
{
    if(!dma_select_stream_timing(&dma_selected_read_a5,
                                 &dma_selected_write_a5,
                                 &dma_selected_frame)) return 0;
    if(dma_selected_frame!=1u)return 0;
    wr(41,dma_selected_read_a5);wr(78,1u);if(rd(78)!=1u)return 0;delay_us(100);
    log_line("DMA_GPIO_A5 delay=%u frame=%u\n",
        dma_selected_read_a5,dma_selected_frame);
    log_line("DMA_GPIO_A5_DIRECTION read=%u write=%u cpu=%u frame=%u\n",
        dma_selected_read_a5,dma_selected_write_a5,dma_cpu_a5,
        dma_selected_frame);
    static const unsigned small_gaps[] = {64u, 8u, 2u, 0u};
    for (unsigned i = 0; i < sizeof(small_gaps)/sizeof(small_gaps[0]); ++i) {
        if (!dma_copy_case(128u, small_gaps[i])) return 0;
    }
    if (!dma_copy_case(4096u, 0u)) return 0;
    if (!dma_copy_case(524288u, 0u)) return 0;
    for(unsigned repeat=0;repeat<3;++repeat) {
        unsigned base=0x00200000u+repeat*0x00200000u;
        unsigned seed=repeat==1?~0x6d2b79f5u:(0x6d2b79f5u^(repeat*0x31415927u));
        if(!dma_large_case(base,2097152u,seed)) return 0;
    }
    return 1;
}

static void dual_bank_diagnostics(void)
{
    alt_irq_disable_all();
    log_line("FULLRATE_CONTROLLER CK/4,4BG\n");
    log_line("PRIMITIVE_DDR_BEGIN backend=frozen-primitive-rtl\n");
#if PUBLIC_MEMORY_CLOCK_MHZ == 1067u
    log_line("PRIMITIVE_DDR_CLOCK exact_ck_hz=3200000000/3 exact_rate_MT_s=6400000000/3 nominal_clock_MHz=1067 native_hz=800000000/3\n");
    log_line("CONTROLLER_COMPATIBILITY status=pending_native2133_generated_platform_and_S54_validation base_read_latency=25 frontend_read_latency=27 trial_profile=1\n");
#elif PUBLIC_MEMORY_CLOCK_MHZ == 800u
    log_line("PRIMITIVE_DDR_CLOCK exact_ck_hz=800000000/1 exact_rate_MT_s=1600/1 nominal_clock_MHz=800 native_hz=200000000/1\n");
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1600u
    log_line("PRIMITIVE_DDR_CLOCK exact_ck_hz=1600000000/1 exact_rate_MT_s=3200/1 nominal_clock_MHz=1600\n");
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1333u
    log_line("PRIMITIVE_DDR_CLOCK exact_ck_hz=4000000000/3 exact_rate_MT_s=8000/3 nominal_clock_MHz=1333\n");
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1467u
    log_line("PRIMITIVE_DDR_CLOCK exact_ck_hz=4400000000/3 exact_rate_MT_s=8800/3 nominal_clock_MHz=1467\n");
#endif
    log_line("PRIMITIVE_DDR_PROFILE ck_MHz=%u rate_MT_s=%s CL%u CWL%u WR%u RTP%u phase=%u\n",
        PUBLIC_MEMORY_CLOCK_MHZ, PUBLIC_DDR_RATE, PUBLIC_DDR_CL, PUBLIC_DDR_CWL,
        PUBLIC_DDR_WR, PUBLIC_DDR_RTP, PUBLIC_COMMAND_PHASE);
#if PUBLIC_MEMORY_CLOCK_MHZ == 1067u || PUBLIC_MEMORY_CLOCK_MHZ == 800u || PUBLIC_MEMORY_CLOCK_MHZ == 1333u || PUBLIC_MEMORY_CLOCK_MHZ == 1467u || PUBLIC_MEMORY_CLOCK_MHZ == 1600u
    log_line("PRIMITIVE_DFI_PHASES manual=%u fast_read=%u fast_write=%u\n",
        PUBLIC_COMMAND_PHASE, PUBLIC_FAST_READ_PHASE, PUBLIC_FAST_WRITE_PHASE);
#endif
    for (unsigned ms = 0; !(rd(0x7f4u/4u) & (1u<<dual_selected_bank)); ms += 10) {
        if (ms >= 10000) fail_forever("PUBLIC_PHY_LOCK_FAIL");
        if (ms % 1000 == 0) log_line("PUBLIC_PHY_LOCK_WAIT ms=%u\n", ms);
        delay_us(10000);
    }

    wr(24, PUBLIC_COMMAND_PHASE);
    wr(PUBLIC_GPIO_A5_DELAY,
        2u);
    wr(25, 3); wr(26, 2); /* Cold image defaults: read gate 3, write tap 2. */
    wr(PUBLIC_READ_MASK, 0x0f);
    initialize_public_rate();

    /* Check the cold public PHY without changing its Vref or delay settings. */
    mode(3, 4);
#if PUBLIC_MEMORY_CLOCK_MHZ >= 1200u || PUBLIC_MEMORY_CLOCK_MHZ == 1067u || PUBLIC_MEMORY_CLOCK_MHZ == 800u
    unsigned cold_gate=PUBLIC_RX_COLD_GATE;
    wr(25,cold_gate);
    public_phy_set_tx_shift(0);
    log_line("PUBLIC_RX_COLD_RESET gate=%u TXDQ=0 TXDQS=32 RX_preserved=1\n",cold_gate);
#endif
#if PUBLIC_MEMORY_CLOCK_MHZ >= 1200u || PUBLIC_MEMORY_CLOCK_MHZ == 1067u || PUBLIC_MEMORY_CLOCK_MHZ == 800u
    unsigned rcven_lanes[4]; dual_data_lanes(rcven_lanes);
    for(unsigned byte=0;byte<4;byte++) {
        public_phy_set_rx_rcven(byte,PUBLIC_RX_RCVEN_CODE);
        unsigned base=dual_cal_base()+rcven_lanes[byte]*0x2000u;
        unsigned rcven_upper=IORD_32DIRECT(base,0x114)&0x7ffu;
        unsigned rcven_lower=IORD_32DIRECT(base,0x11c)&0x7ffu;
        if(rcven_upper!=PUBLIC_RX_RCVEN_CODE || rcven_lower!=PUBLIC_RX_RCVEN_CODE)fail_forever("PUBLIC_RX_RCVEN_CONFIG_FAIL");
        log_line("PUBLIC_RX_RCVEN lane=%u upper=%u lower=%u RX_PI_preserved=1\n",rcven_lanes[byte],rcven_upper,rcven_lower);
        log_line("PUBLIC_RX_RCVEN_CHECK stage=pre_tx byte=%u lane=%u upper=%u lower=%u\n",byte,rcven_lanes[byte],rcven_upper,rcven_lower);
    }
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1100u
    public_phy_set_rx_rcven(0,64);
    unsigned rcven_upper=IORD_32DIRECT((dual_cal_base()+0x8000u),0x114)&0x7ffu;
    unsigned rcven_lower=IORD_32DIRECT((dual_cal_base()+0x8000u),0x11c)&0x7ffu;
    if(rcven_upper!=PUBLIC_RX_RCVEN_CODE || rcven_lower!=PUBLIC_RX_RCVEN_CODE)fail_forever("PUBLIC_RX_RCVEN_CONFIG_FAIL");
    log_line("PUBLIC_RX_RCVEN lane=4 upper=%u lower=%u RX_PI_preserved=1\n",rcven_upper,rcven_lower);
    log_line("PUBLIC_RX_RCVEN_CHECK stage=pre_tx upper=%u lower=%u\n",rcven_upper,rcven_lower);
#endif
    unsigned mpr_errors = 0, valid_zero = 0, valid_ones = 0;
    mpr_errors += check_mpr_pattern(0, &valid_zero);
    mpr_errors += check_mpr_pattern(0xffffffffu, &valid_ones);
    log_line("PUBLIC_MPR_CHECK errors=%u zero_valid=%04lx ff_valid=%04lx\n",
        mpr_errors, (unsigned long)valid_zero, (unsigned long)valid_ones);
    if (mpr_errors) fail_forever("PUBLIC_MPR_CHECK_FAIL");

    primitive_set_training_tx();
#if PUBLIC_MEMORY_CLOCK_MHZ >= 1200u || PUBLIC_MEMORY_CLOCK_MHZ == 1067u || PUBLIC_MEMORY_CLOCK_MHZ == 800u
    for(unsigned byte=0;byte<4;byte++) {
        unsigned base=dual_cal_base()+rcven_lanes[byte]*0x2000u;
        unsigned rcven_upper=IORD_32DIRECT(base,0x114)&0x7ffu;
        unsigned rcven_lower=IORD_32DIRECT(base,0x11c)&0x7ffu;
        if(rcven_upper!=PUBLIC_RX_RCVEN_CODE || rcven_lower!=PUBLIC_RX_RCVEN_CODE)fail_forever("PUBLIC_RX_RCVEN_RECAL_FAIL");
        log_line("PUBLIC_RX_RCVEN_CHECK stage=post_tx byte=%u lane=%u upper=%u lower=%u\n",byte,rcven_lanes[byte],rcven_upper,rcven_lower);
    }
#elif PUBLIC_MEMORY_CLOCK_MHZ == 1100u
    rcven_upper=IORD_32DIRECT((dual_cal_base()+0x8000u),0x114)&0x7ffu;
    rcven_lower=IORD_32DIRECT((dual_cal_base()+0x8000u),0x11c)&0x7ffu;
    if(rcven_upper!=PUBLIC_RX_RCVEN_CODE || rcven_lower!=PUBLIC_RX_RCVEN_CODE)fail_forever("PUBLIC_RX_RCVEN_RECAL_FAIL");
    log_line("PUBLIC_RX_RCVEN_CHECK stage=post_tx upper=%u lower=%u\n",rcven_upper,rcven_lower);
#endif
    unsigned recal_errors=check_mpr_pattern(0,0)+check_mpr_pattern(0xffffffffu,0);
    log_line("PUBLIC_RECAL_MPR errors=%u\n",recal_errors);
    if(recal_errors)fail_forever("PUBLIC_RECAL_MPR_FAIL");
    log_line("PUBLIC_TX_SHIFT DQ=64 DQS_default=96 byte1_DQS=112 RX_preserved=1\n");
    mode(3, 0);
    wr(78,1); fence();
    if(rd(78)!=1u)fail_forever("NATIVE2133_ALIGNMENT1_CONFIG_FAIL");
    wr(1, 15); /* Hand command, CKE and reset control to LiteDRAM. */
    unsigned trained = 0, chosen_w = 0, chosen_r = 0, chosen_gpio = 0;
    log_line("NATIVE2133_ALIGNMENT policy=1 only stage=native_training_cpu_DMA_console model_profile=base25_frontend27_trial\n");
    log_line("NATIVE2133_READ_AGE policy=counter_delta_1_or_2 first_age_16_to_18 last_minus_first=delta_minus_1 scope=serialized_CPU_only alignment=1\n");
    static const unsigned write_taps[5]={4,3,2,5,6};
    for (unsigned wi=0;wi<5 && !trained;wi++) {
        unsigned w=write_taps[wi];
        wr(26, w);
        #if PUBLIC_MEMORY_CLOCK_MHZ == 1067u || PUBLIC_MEMORY_CLOCK_MHZ == 800u || PUBLIC_MEMORY_CLOCK_MHZ == 1333u || PUBLIC_MEMORY_CLOCK_MHZ == 1467u || PUBLIC_MEMORY_CLOCK_MHZ == 1600u
        static const unsigned read_taps[6]={3,4,5,1,2,6};
        #elif PUBLIC_MEMORY_CLOCK_MHZ >= 1200u
        static const unsigned read_taps[6]={4,3,1,2,5,6};
#else
        static const unsigned read_taps[6]={3,1,2,4,5,6};
#endif
        for (unsigned ri = 0; ri < 6 && !trained; ++ri) {
            unsigned r=read_taps[ri];
            wr(25, r);
            #if PUBLIC_MEMORY_CLOCK_MHZ >= 1200u || PUBLIC_MEMORY_CLOCK_MHZ == 1067u || PUBLIC_MEMORY_CLOCK_MHZ == 800u
            static const unsigned gpio_taps[4]={2,1,3,4};
#else
            static const unsigned gpio_taps[5]={2,0,1,3,4};
#endif
            for (unsigned gi = 0; gi < sizeof(gpio_taps)/sizeof(gpio_taps[0]) && !trained; ++gi) {
                unsigned gpio=gpio_taps[gi];
                wr(PUBLIC_GPIO_A5_DELAY, gpio);
                wr(1,3);primitive_set_training_tx();
                wr(78,1);fence();
                if(rd(78)!=1u)fail_forever("NATIVE2133_ALIGNMENT1_RECENTER_FAIL");
                wr(1,15);
                delay_us(2);
                unsigned errors = 0;
                /* Separated rows, banks and columns; each beat changes every
                 * DQ bit. Complement pass catches stuck-high and stuck-low. */
                static const uint32_t probe_addresses[45] = {
                    0,3,7,15,31,63,127,255,511,1023,2047,4095,8191,16383,65535,0x12345,
                    1u<<0,1u<<1,1u<<2,1u<<3,1u<<4,1u<<5,1u<<6,1u<<7,
                    1u<<8,1u<<9,1u<<10,1u<<11,1u<<12,1u<<13,1u<<14,
                    1u<<15,1u<<16,1u<<17,1u<<18,1u<<19,1u<<20,1u<<21,
                    1u<<22,1u<<23,1u<<24,1u<<25,1u<<26,1u<<27,0x0fffffffu
                };
                if (wi == 0 && ri == 0 && gi == 0)
                    log_line("PRIM_TRAIN_COVERAGE addresses=45 address_bits=28 reverse_write=1 mixed=1\n");
                for (unsigned pass=0; pass<2; ++pass) {
                    /* Reverse writes on the complement pass to expose aliases
                     * that depend on which colliding address was written last. */
                    for (unsigned n=0;n<45;++n) {
                        unsigned p=pass ? 44u-n : n;
                        uint32_t a=probe_addresses[p];
                        for(unsigned i=0;i<8;i++)wr(8+i,training_word(a,i,pass));
                        errors+=!operation_checked(a,1);
                    }
                    for (unsigned p=0;p<45;++p) {
                        uint32_t a=probe_addresses[p];
                        errors+=!operation_checked(a,0);
                        if(rd(27)!=0xffffu)errors++;
                        for(unsigned i=0;i<8;i++) {
                            uint32_t expected=training_word(a,i,pass), actual=rd(16+i);
                            if(actual!=expected) {
                                if(errors<PUBLIC_MAX_ERRORS)
                                    log_line("PRIM_TRAIN_MISMATCH p=%u a=%lu i=%u e=%08lx g=%08lx\n",
                                        pass,(unsigned long)a,i,(unsigned long)expected,(unsigned long)actual);
                                errors++;
                            }
                        }
                    }
                }
                uint32_t flags = rd(PUBLIC_CMD_FLAGS);
                if (flags) errors++;
                log_line("PRIM_TRAIN w=%u r=%u a5=%u errors=%u flags=%lx first=%lu last=%lu\n",
                    w, r, gpio, errors, (unsigned long)flags, (unsigned long)rd(43), (unsigned long)rd(44));
                if (!errors) {
                    trained = 1; chosen_w = w; chosen_r = r; chosen_gpio = gpio;
                }
            }
        }
    }
    if (!trained) fail_forever("PUBLIC_LITEDRAM_TRAIN_FAIL");
    log_line("PRIMITIVE_DDR_TRAIN_PASS w=%u r=%u a5=%u\n",
        chosen_w, chosen_r, chosen_gpio);
    dma_cpu_a5=chosen_gpio;

    static const unsigned bytes_per_pattern = 1024u * 1024u;
    static const unsigned bursts_per_pattern = bytes_per_pattern / 32u;
    unsigned total_errors = 0;
    for (unsigned m = 0; m < 6; ++m) {
        unsigned errors = 0;
        for (unsigned a = 0; a < bursts_per_pattern; ++a) {
            for (unsigned i = 0; i < 8; ++i)
                wr(8 + i, pattern(m, a * 8 + i));
            if (!operation_checked(a, 1)) errors++;
        }
        for (unsigned a = 0; a < bursts_per_pattern; ++a) {
            if (!operation_checked(a, 0)) errors++;
            unsigned valid = rd(27);
            if (valid != 0xffffu) errors++;
            for (unsigned i = 0; i < 8; ++i) {
                uint32_t expected = pattern(m, a * 8 + i);
                uint32_t actual = rd(16 + i);
                if (actual != expected) {
                    if (errors < PUBLIC_MAX_ERRORS)
                        log_line("PUB_MISMATCH m=%u a=%u i=%u e=%08lx g=%08lx\n",
                            m, a, i, (unsigned long)expected, (unsigned long)actual);
                    errors++;
                }
            }
        }
        total_errors += errors;
        log_line("PUBLIC_LITEDRAM_PATTERN mode=%u bytes=%u errors=%u\n",
            m, bytes_per_pattern, errors);
    }
    /* Write every sparse location first, then verify after a refresh interval.
     * Burst addresses are 28 bits: 32-byte bursts cover the 8 GiB geometry.
     */
    uint32_t sparse[30];sparse[0]=0;
    for(unsigned bit=0;bit<28;bit++)sparse[bit+1]=1u<<bit;
    sparse[29]=0x0fffffffu;
    unsigned sparse_errors=0;
    for(unsigned n=0;n<30;n++) {
        for(unsigned i=0;i<8;i++)wr(8+i,pattern(4,sparse[n]*8+i)^0x13579bdfu);
        sparse_errors+=!operation_checked(sparse[n],1);
    }
    delay_us(250000);
    for(unsigned n=0;n<30;n++) {
        sparse_errors+=!operation_checked(sparse[n],0);
        for(unsigned i=0;i<8;i++) {
            unsigned expected=pattern(4,sparse[n]*8+i)^0x13579bdfu;
            unsigned actual=rd(16+i);
            if(actual!=expected) {
                if(sparse_errors<4)log_line("SPARSE_MISMATCH a=%08lx i=%u e=%08lx g=%08lx\n",(unsigned long)sparse[n],i,(unsigned long)expected,(unsigned long)actual);
                sparse_errors++;
            }
        }
    }
    total_errors+=sparse_errors;
    log_line("PUBLIC_LITEDRAM_SPARSE bytes=960 locations=30 range_GiB=8 retention_ms=250 errors=%u\n",sparse_errors);
    log_line("PUBLIC_LITEDRAM_MEM_%s bytes=%u patterns=6 errors=%u\n",
        total_errors ? "FAIL" : "PASS", bytes_per_pattern * 6u, total_errors);
    if (total_errors) fail_forever("PUBLIC_LITEDRAM_MEM_FAIL");
    if (!dma_ramp()) fail_forever("DMA_RAMP_FAIL");
    log_line("DMA_RAMP_PASS\n");
    return;
}

static unsigned dual_cpu_a5[2],dual_read_a5[2],dual_write_a5[2],dual_frame[2];
static unsigned dual_full_scan;
static void dual_select(unsigned bank)
{
    if(bank>1)fail_forever("DUAL_INVALID_BANK_FAIL");
    wr(0x7f0u/4u,bank);
    uint64_t started=alt_niosv_mtime_get();
    while(rd(0x7f0u/4u)!=bank) {
        if(alt_niosv_mtime_get()-started>ALT_CPU_CPU_FREQ/100u)
            fail_forever("DUAL_BANK_SELECT_FAIL");
    }
    dual_selected_bank=bank;
}
static void dual_start(unsigned bank,unsigned mode,unsigned base,unsigned words,unsigned seed)
{
    dual_select(bank);
    if(dual_frame[bank]!=1u)fail_forever("NATIVE2133_ALIGNMENT1_DMA_FRAME_FAIL");
    wr(41,mode?dual_read_a5[bank]:dual_write_a5[bank]);
    wr(78,1u);if(rd(78)!=1u)fail_forever("NATIVE2133_ALIGNMENT1_DMA_READBACK_FAIL");
    wr(DMA_CONTROL,0);wr(DMA_BASE,base);wr(DMA_LENGTH,words);
    wr(DMA_MODE,mode);wr(DMA_SEED,seed);wr(DMA_GAP,0);wr(DMA_CONTROL,1);
}
static void dual_collect(unsigned bank,unsigned mode,unsigned words)
{
    dual_select(bank);
    unsigned status=rd(DMA_STATUS),errors=rd(DMA_ERRORS);
    unsigned issued=rd(DMA_ISSUED),moved=rd(DMA_TRANSFERRED);
    unsigned rc=rd(DMA_DIAG_READ_CAS),wc=rd(DMA_DIAG_WRITE_CAS);
    unsigned bad=!(status&2u)||(status&4u)||errors||issued!=words||moved!=words;
    bad|=rd(DMA_DIAG_STRIPED_ERRORS)!=0||rd(PUBLIC_CMD_FLAGS)!=0;
    bad|=mode?rc!=words:wc!=words;
    log_line("%s_%s mode=%s words=%u issued=%u moved=%u errors=%u cycles=%08lx%08lx read_cas=%u write_cas=%u\n",
        dual_full_scan?"DUAL_FULL_DMA":"DUAL_DMA",bad?"FAIL":"PASS",mode?"R":"W",words,issued,moved,errors,
        (unsigned long)rd(DMA_CYCLES_HI),(unsigned long)rd(DMA_CYCLES_LO),rc,wc);
    if(bad)fail_forever("DUAL_CONCURRENT_DMA_FAIL");
}
static void dual_concurrent_case(unsigned base,unsigned words,unsigned round)
{
    unsigned seed[2]={0x6d2b79f5u^round,0xa5c39e71u^round};
    log_line("%s_CASE base=%08lx words=%u round=%u\n",
        dual_full_scan?"DUAL_FULL_DMA":"DUAL_DMA",(unsigned long)base,words,round);
    for(unsigned mode=0;mode<2;mode++) {
        dual_start(0,mode,base,words,seed[0]);
        dual_start(1,mode,base,words,seed[1]);
        dual_select(0);
        unsigned status0=rd(DMA_STATUS);
        dual_select(1);
        unsigned status1=rd(DMA_STATUS);
        dual_select(0);
        unsigned overlap=(status0&1u)&&(status1&1u)&&!(status0&6u)&&!(status1&6u);
        log_line("%s_OVERLAP mode=%s observed=%u busy0=%u busy1=%u\n",
            dual_full_scan?"DUAL_FULL_DMA":"DUAL_DMA",mode?"R":"W",overlap,
            !!(status0&1u),!!(status1&1u));
        if(!overlap)fail_forever("DUAL_DMA_NO_OVERLAP_FAIL");
        uint64_t started=alt_niosv_mtime_get();
        unsigned done[2]={0,0};
        while(!done[0]||!done[1]) {
            for(unsigned bank=0;bank<2;bank++) {
                dual_select(bank);
                unsigned status=rd(DMA_STATUS);
                if(status&4u)fail_forever("DUAL_DMA_STATUS_FAIL");
                done[bank]=(status&2u)!=0;
            }
            if(alt_niosv_mtime_get()-started>ALT_CPU_CPU_FREQ*5ull)
                fail_forever("DUAL_DMA_TIMEOUT_FAIL");
        }
        dual_collect(0,mode,words);dual_collect(1,mode,words);
        if(!mode) {
            for(unsigned bank=0;bank<2;bank++) {
                dual_select(bank);
                if(!dma_cpu_verify_at_tap(base,words,seed[bank],dual_cpu_a5[bank]))
                    fail_forever("DUAL_DMA_CPU_READBACK_FAIL");
            }
        }
    }
}
static void dual_sparse_isolation(void)
{
    unsigned addresses[30];addresses[0]=0;
    for(unsigned bit=0;bit<28;bit++)addresses[bit+1]=1u<<bit;
    addresses[29]=0x0fffffffu;
    for(unsigned bank=0;bank<2;bank++) {
        dual_select(bank);wr(41,dual_cpu_a5[bank]);
        for(unsigned j=0;j<30;j++) {
            for(unsigned lane=0;lane<8;lane++)
                wr(8+lane,training_word(addresses[j],lane,bank));
            if(!operation_checked(addresses[j],1))fail_forever("DUAL_SPARSE_WRITE_FAIL");
        }
    }
    delay_us(250000);
    for(unsigned bank=0;bank<2;bank++) {
        dual_select(bank);wr(41,dual_cpu_a5[bank]);
        unsigned errors=0;
        for(unsigned j=0;j<30;j++) {
            errors+=!operation_checked(addresses[j],0);
            errors+=rd(27)!=0xffffu;
            for(unsigned lane=0;lane<8;lane++)
                errors+=rd(16+lane)!=training_word(addresses[j],lane,bank);
        }
        log_line("DUAL_SPARSE_%s locations=30 range_GiB=8 retention_ms=250 errors=%u\n",
            errors?"FAIL":"PASS",errors);
        if(errors)fail_forever("DUAL_BANK_ISOLATION_FAIL");
    }
}
int main(void)
{
    alt_irq_disable_all();
    log_line("DUAL_DDR_BEGIN banks=2 capacity_GiB=16 rate_MT_s=%s ecc=disabled\n",PUBLIC_DDR_RATE);
    log_line("FW14_DIAGNOSTIC_ORDER banks=0,1 parent=FW7 profile=base25_frontend27 fail_stop=unchanged\n");
    static const unsigned diagnostic_order[2]={0u,1u};
    for(unsigned index=0;index<2;index++) {
        unsigned bank=diagnostic_order[index];
        dual_select(bank);
        dma_calibration_mode=0;dma_cpu_a5=2;dma_selected_read_a5=2;
        dma_selected_write_a5=2;dma_selected_frame=1;
        dual_bank_diagnostics();
        dual_cpu_a5[bank]=dma_cpu_a5;dual_read_a5[bank]=dma_selected_read_a5;
        dual_write_a5[bank]=dma_selected_write_a5;dual_frame[bank]=dma_selected_frame;
        log_line("DUAL_BANK_DIAGNOSTIC_PASS\n");
    }
    dual_sparse_isolation();
    for(unsigned round=0;round<3;round++)
        dual_concurrent_case(0x00200000u+round*0x00200000u,2097152u,round);
    dual_full_scan=1;
    for(unsigned segment=0;segment<8;segment++)
        dual_concurrent_case(segment*33554432u,33554432u,segment+16u);
    dual_select(0);
    log_line("DUAL_DDR_DIAGNOSTIC_PASS banks=2 concurrent_cases=3 full_scan_GiB_per_bank=8\n");
    dual_select(0);
    log_line("DUAL_DDR_IDLE status=memory_diagnostic_complete native2133_profile_unqualified\n");
    for(;;)delay_us(100000);
}
