module add_sub4(input [3:0] a,b, output [4:0] sum, output [3:0] difference);
  assign sum = {1'b0,a} + {1'b0,b};
  assign difference = a - b;
endmodule
