module unused_state_output(input clk,rst,en,input [7:0] a,b,output [7:0] q_a,q_b,hidden_a);
hier_dual first(.clk(clk),.rst(rst),.en(en),.d(a),.visible(q_a),.hidden(hidden_a));
hier_dual second(.clk(clk),.rst(rst),.en(en),.d(b),.visible(q_b),.hidden());
endmodule
module hier_dual(input clk,rst,en,input [7:0] d,output reg [7:0] visible,hidden);
always @(posedge clk) begin
  if(rst) begin visible<=8'h55; hidden<=8'haa; end
  else if(en) begin visible<=d; hidden<=~d; end
end
endmodule
