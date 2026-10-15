"""V6: same-edge prequalified PHY configuration write requests."""

from migen import *


class FullRateCSRRequestPipeline(Module):
    """V1 request pipeline with a same-edge registered write polarity.

    ``request_write`` is captured alongside ``request_read`` when the public
    request enters the pipeline. While ``request_valid`` is asserted, it is
    exactly the complement of ``request_read``. It supplies the write side of
    the internal interface so CSR write-enable cones need not distribute the
    read flag and invert it at each consumer.
    """

    def __init__(self, external, internal):
        self.flush = flush = Signal()

        self.busy = busy = Signal(reset=0)
        self.request_valid = request_valid = Signal(reset=0)
        self.request_read = request_read = Signal(reset=0)
        self.request_write = request_write = Signal(reset=0)
        self.read_waiting = read_waiting = Signal(reset=0)
        self.address_select = address_select = Signal(128, reset=1)
        address_select.attr.add("no_retiming")
        address = Signal(len(external.address), reset_less=True)
        writedata = Signal(len(external.writedata), reset_less=True)
        readdata = Signal(len(external.readdata), reset_less=True)
        readdatavalid = Signal(reset=0)
        for signal in (busy, request_valid, request_read, read_waiting,
                       address, writedata, readdata, readdatavalid):
            signal.attr.add("no_retiming")
        request_write.attr.add("preserve_syn_only")
        self.phy_config_write = {n: Signal(name=f"phy_config_write_{n}")
                                 for n in (25, 26, 40, 41, 78)}
        for active in self.phy_config_write.values():
            active.attr.add("preserve_syn_only")

        internal_accept = request_valid & ~internal.waitrequest
        response_for_current_read = read_waiting & internal.readdatavalid
        response_on_accept = (request_valid & request_read &
            ~internal.waitrequest & internal.readdatavalid)
        response_received = response_for_current_read | response_on_accept

        self.comb += [
            external.waitrequest.eq(busy | flush),
            external.readdata.eq(readdata),
            external.readdatavalid.eq(readdatavalid),
            internal.address.eq(address),
            internal.writedata.eq(writedata),
            internal.read.eq(request_valid & request_read & ~flush),
            internal.write.eq(request_valid & request_write & ~flush),
        ]

        self.sync += If(flush,
            busy.eq(0), request_valid.eq(0), request_read.eq(0),
            request_write.eq(0), read_waiting.eq(0), readdatavalid.eq(0),
            *[active.eq(0) for active in self.phy_config_write.values()]
        )
        self.sync += If(~flush,
                readdatavalid.eq(0),
                If(~busy & (external.read | external.write),
                    busy.eq(1), request_valid.eq(1),
                    request_read.eq(external.read),
                    request_write.eq(~external.read),
                    *[active.eq(~external.read & (external.address[2:] == n))
                      for n, active in self.phy_config_write.items()],
                    address.eq(external.address),
                    writedata.eq(external.writedata)
                ),
                If(internal_accept,
                    request_valid.eq(0),
                    *[active.eq(0) for active in self.phy_config_write.values()],
                    If(request_read,
                        read_waiting.eq(1)
                    ).Else(
                        busy.eq(0)
                    )
                ),
                If(response_received,
                    readdata.eq(internal.readdata),
                    readdatavalid.eq(1), busy.eq(0),
                    request_valid.eq(0), read_waiting.eq(0),
                    *[active.eq(0) for active in self.phy_config_write.values()]
                )
            )
        decode_enable = ~busy & ~flush
        self.sync += If(decode_enable,
            *[address_select[i].eq(external.address[2:] == i)
              for i in range(128)])

    def phy_write_request(self, address):
        return self.phy_config_write[address] & ~self.flush
