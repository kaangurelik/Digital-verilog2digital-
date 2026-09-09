module fsm(input clk, rst, go, back, output reg [1:0] state, output busy);
always @(posedge clk)
    if (rst) state <= 2'd2;
    else case (state)
        2'd0: if (go) state <= 2'd1;
        2'd1: if (go) state <= 2'd2; else if (back) state <= 2'd0;
        2'd2: if (back) state <= 2'd1;
        default: state <= 2'd0;
    endcase
assign busy = state != 2'd0;
endmodule
