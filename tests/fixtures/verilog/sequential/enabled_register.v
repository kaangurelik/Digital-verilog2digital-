module enabled_register(input clk, en, input [7:0] d, output reg [7:0] q);
always @(posedge clk) if (en) q <= d;
endmodule
