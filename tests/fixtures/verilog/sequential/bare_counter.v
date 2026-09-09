module bare_counter(input clk, output reg [3:0] q);
always @(posedge clk) q <= q + 4'd1;
endmodule
