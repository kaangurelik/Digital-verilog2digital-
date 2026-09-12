module hidden_child_state(input clk,rst,en,input [7:0] d,output [7:0] q);
hier_hidden child(.clk(clk),.rst(rst),.en(en),.d(d),.visible(q),.hidden());
endmodule
module hier_hidden(input clk,rst,en,input [7:0] d,output reg [7:0] visible,output reg [3:0] hidden);
always @(posedge clk) begin
  if(rst) visible<=8'd0; else if(en) visible<=d;
  hidden<=hidden+4'd1;
end
endmodule
