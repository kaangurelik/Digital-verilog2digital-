module simultaneous(input clk, rst, output reg [7:0] a, b);
always @(posedge clk) begin
    if (rst) begin a <= 8'ha5; b <= 8'h3c; end
    else begin a <= b; b <= a; end
end
endmodule
