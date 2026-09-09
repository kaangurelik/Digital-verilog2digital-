module constant_register(input clk, output reg [7:0] q);
always @(posedge clk) q <= 8'd0;
endmodule
