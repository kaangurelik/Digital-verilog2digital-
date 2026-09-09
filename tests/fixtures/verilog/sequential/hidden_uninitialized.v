module hidden_uninitialized(input clk, expose, output [3:0] y);
reg [3:0] state;
always @(posedge clk) state <= state + 4'd1;
assign y = expose ? state : 4'd0;
endmodule
