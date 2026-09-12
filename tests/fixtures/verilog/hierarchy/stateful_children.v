module stateful_children(input clk,rst,en_a,en_b,input [7:0] d_a,d_b,output [7:0] q_a,q_b);
hier_register first(.clk(clk),.rst(rst),.en(en_a),.d(d_a),.q(q_a));
hier_register second(.clk(clk),.rst(rst),.en(en_b),.d(d_b),.q(q_b));
endmodule
module hier_register(input clk,rst,en,input [7:0] d,output reg [7:0] q);
always @(posedge clk) if(rst) q<=8'h3c; else if(en) q<=d;
endmodule
