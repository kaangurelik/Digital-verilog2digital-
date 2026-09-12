module controller_datapath(input clk,rst,step,input [3:0] d,output [3:0] q,output busy);
hier_controller control(.clk(clk),.rst(rst),.step(step),.enable(busy));
hier_accumulator datapath(.clk(clk),.rst(rst),.enable(busy),.d(d),.q(q));
endmodule
module hier_controller(input clk,rst,step,output enable);
reg [1:0] state;
always @(posedge clk) if(rst) state<=2'd0; else if(step) state<=state+2'd1;
assign enable=state[0];
endmodule
module hier_accumulator(input clk,rst,enable,input [3:0] d,output reg [3:0] q);
always @(posedge clk) if(rst) q<=4'd6; else if(enable) q<=q+d;
endmodule
