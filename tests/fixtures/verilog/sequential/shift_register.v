module shift_register(input clk, d, output reg [3:0] q);
always @(posedge clk) q <= {q[2:0], d};
endmodule
