`define RELEASE_HEADER "ölçü_π.vh"
`include `RELEASE_HEADER
module unicode_include(input [`RELEASE_WIDTH-1:0] a, output [`RELEASE_WIDTH-1:0] y);
assign y = a ^ `RELEASE_MASK;
endmodule
