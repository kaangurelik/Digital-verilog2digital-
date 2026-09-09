module active_low_reset(input clk, rst_n, input [7:0] d, output reg [7:0] q);
always @(posedge clk)
    if (!rst_n) q <= 8'ha6;
    else q <= d;
endmodule
