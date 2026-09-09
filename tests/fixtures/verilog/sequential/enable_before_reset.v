module enable_before_reset(input clk, rst, en, input [7:0] d, output reg [7:0] q);
always @(posedge clk)
    if (en) begin
        if (rst) q <= 8'h55;
        else q <= d;
    end
endmodule
