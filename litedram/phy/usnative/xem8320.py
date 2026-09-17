#
# This file is part of LiteDRAM.
#
# SPDX-License-Identifier: BSD-2-Clause

"""Explicit native serializer wiring for the initial XEM8320 firmware ABI.

Physical connections come from validated device queries. No reference netlist,
generated port manifest, or board import is required. The sparse tap numbering
is retained for compatibility with the initial BIOS profile; it is deliberately
restricted to that profile until firmware supports logical lane numbering.
"""
import re

from migen import Signal, Instance, Replicate

def core_ports():
    ports = {}
    def add(direction, width, *names):
        for name in names:
            ports[name] = (direction, width)
    add('input', 1, 'i_div_clk', 'i_en_vtc', 'i_pll_clk', 'i_riu_clk', 'i_rst',
        'i_clb2phy_tristate_odelay_rst')
    add('input', 26, 'i_fifo_rd_en')
    add('input', 16, 'i_phy_rden', 'i_riu_wr_data')
    add('input', 6, 'i_riu_addr')
    add('input', 8, 'i_riu_nibble_sel', 'i_data_tbyte')
    add('input', 4, 'i_riu_wr_en', 'i_cmd_tbyte')
    add('output', 8, 'o_dly_rdy', 'o_vtc_rdy', 'o_riu_valid')
    add('output', 128, 'o_riu_rd_data')
    add('output', 27, 'o_dyn_dci')
    for name, width in [('dq', 16), ('dm_n', 2), ('dqs_t', 2), ('adr', 17),
                        ('ba', 2), ('bg', 1), ('cke', 1), ('odt', 1),
                        ('cs_n', 1), ('act_n', 1), ('ck_t', 1)]:
        add('output', width, f'o_{name}_serial_out')
        if name in ('dq', 'dm_n', 'dqs_t'):
            add('output', width, f'o_{name}_tristate')
            add('input', width, f'i_{name}_serial_in')
        if name in ('dq', 'dm_n'):
            add('output', 8*width, f'o_{name}_rx_data')
        if name not in ('dqs_t', 'ck_t'):
            add('input', 8*width, f'i_{name}_tx_data')
    return ports


def signal_sites(sites):
    result = {}
    aliases = {'a': 'adr', 'dm': 'dm_n', 'dqs_p': 'dqs_t', 'clk_p': 'ck_t'}
    for (signal, index), site in sites.items():
        if signal in ('dqs_n', 'clk_n', 'reset_n'):
            continue
        if signal in ('we_n', 'cas_n', 'ras_n'):
            index = 14 + ('we_n', 'cas_n', 'ras_n').index(signal)
            signal = 'a'
        match = re.fullmatch(r'BITSLICE_RX_TX_X0Y(\d+)', site.native_site)
        if not match or site.bank != 64:
            raise ValueError('Initial native firmware requires the XEM8320 bank-64 profile')
        number = int(match[1])
        if number >= 52:
            raise ValueError('Native tap index exceeds the firmware ABI')
        result[f'o_{aliases.get(signal, signal)}_serial_out[{index}]'] = number
    if len(result) != 45 or len(set(result.values())) != 45:
        raise ValueError('Initial native profile requires 45 distinct serializers')
    return result


def riu_bindings(auxiliary):
    groups = {}
    for control, site in auxiliary.items():
        match = re.fullmatch(r'BITSLICE_CONTROL_X0Y([0-7])', control)
        if not match:
            raise ValueError('Unsupported native control ABI')
        groups.setdefault(site.riu, {})[site.riu_input] = int(match[1])
    if len(groups) != 4 or any(set(v) != {'LOW', 'UPP'} for v in groups.values()):
        raise ValueError('Expected four complete RIU byte pairs')
    result = [(riu, sides['LOW'], sides['UPP']) for riu, sides in
              sorted(groups.items(), key=lambda item: min(item[1].values()))]
    if [(lo, hi) for _, lo, hi in result] != [(0, 1), (2, 3), (4, 5), (6, 7)]:
        raise ValueError('RIU byte ordering differs from the firmware profile')
    return result


def connect_core(module, core, sites, boundary):
    """Translate the sparse initial firmware ABI to the portable core layout.

    This adapter packs logical signals only. The portable core owns all device
    interconnect, native site selection and RIU byte routing.
    """
    native = {name: Signal(width, name='core_' + name) for name, (_, width) in core.ports.items()}
    pins = {name[2:]: value for name, value in boundary.items()}
    indices = signal_sites(sites)
    inputs = dict(pll_clk='i_pll_clk', fifo_clk='i_div_clk', riu_clk='i_riu_clk',
        slice_reset='i_clb2phy_tristate_odelay_rst', control_reset='i_rst',
        delay_reset='i_clb2phy_tristate_odelay_rst', slice_vtc='i_slice_en_vtc',
        control_vtc='i_en_vtc', riu_addr='i_riu_addr', riu_wdata='i_riu_wr_data')
    for target, source in inputs.items():
        module.comb += native[target].eq(pins[source])
    module.comb += native['data_tristate'].eq(Replicate(pins['i_data_tristate'], 2))
    # The portable core qualifies the common write strobe by each control's
    # one-hot RIU select. The legacy boundary supplied four qualified strobes.
    module.comb += native['riu_write'].eq(pins['i_riu_wr_en'] != 0)
    for i, control in enumerate(core.layout.controls):
        n = int(control.rsplit('Y', 1)[1])
        module.comb += [
            native['riu_select'][i].eq(pins['i_riu_nibble_sel'][n]),
            pins['o_dly_rdy'][n].eq(native['dly_ready'][i]),
            pins['o_vtc_rdy'][n].eq(native['vtc_ready'][i]),
            native['phy_rden'][4*i:4*i+4].eq(pins['i_phy_rden'][4*n:4*n+4] if n < 4 else 0),
            native['tbyte'][4*i:4*i+4].eq(pins['i_data_tbyte'][4*(n%2):4*(n%2)+4]
                                        if n < 4 else pins['i_cmd_tbyte'])]
    for i, (signal, index) in enumerate(core.layout.slices):
        name = {'a': 'adr', 'dm': 'dm_n', 'dqs_p': 'dqs_t', 'clk_p': 'ck_t'}.get(signal, signal)
        if signal in ('we_n', 'cas_n', 'ras_n'):
            name, index = 'adr', 14 + ('we_n', 'cas_n', 'ras_n').index(signal)
        y = indices[f'o_{name}_serial_out[{index}]']
        receive = signal in ('dq', 'dm', 'dqs_p')
        data = (0x55 if signal == 'clk_p' else pins['i_dqs_tx_data'][8*index:8*index+8]
                if signal == 'dqs_p' else pins[f'i_{name}_tx_data'][8*index:8*index+8])
        module.comb += [
            native['tx_data'][8*i:8*i+8].eq(data),
            pins[f'o_{name}_serial_out'][index].eq(native['serial_out'][i]),
            native['serial_in'][i].eq(pins[f'i_{name}_serial_in'][index] if receive else 0),
            native['fifo_rd_en'][i].eq(pins['i_fifo_rd_en'][y] if y < 26 else 0)]
        if receive:
            module.comb += pins[f'o_{name}_tristate'][index].eq(native['tristate'][i])
        if signal in ('dq', 'dm'):
            module.comb += pins[f'o_{name}_rx_data'][8*index:8*index+8].eq(native['rx_data'][8*i:8*i+8])
            site = sites[signal, index]
            ci = core.layout.controls.index(site.control_site)
            slot = site.position if site.nibble == 'L' else site.position - 6
            di = index if signal == 'dq' else 16+index
            module.comb += pins['o_dyn_dci'][di].eq(native['dyn_dci'][7*ci+slot])
        if y < 26:
            module.comb += pins['o_fifo_empty'][y].eq(native['fifo_empty'][i])
        for side in ('rx', 'tx'):
            module.comb += [
                native[side+'_rst'][i].eq(pins[f'i_{side}_delay_rst'][y]),
                native[side+'_ce'][i].eq(pins[f'i_{side}_delay_ce'][y]),
                pins[f'o_{side}_delay_count'][9*y:9*y+9].eq(native[side+'_count'][9*i:9*i+9])]
    for y in set(range(52)) - set(indices.values()):
        for side in ('rx', 'tx'):
            module.comb += pins[f'o_{side}_delay_count'][9*y:9*y+9].eq(0)
        if y < 26:
            module.comb += pins['o_fifo_empty'][y].eq(1)
    module.specials += Instance('usnative_core', **{
        ('i_' if direction == 'input' else 'o_') + name: native[name]
        for name, (direction, _) in core.ports.items()})
    return native['riu_rdata'], native['riu_valid']
