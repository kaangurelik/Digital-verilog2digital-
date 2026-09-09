module wide_register64(input clk, en, input [63:0] d, output reg [63:0] q);
always @(posedge clk) if (en) q <= d;
endmodule
