module high_fanout(input enable,input [31:0] data,output [31:0] y);
  assign y = data ^ {32{enable}};
endmodule
