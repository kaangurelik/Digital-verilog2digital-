module bus_repeats64(input [7:0] a,output [63:0] repeated);
  assign repeated = {8{a}};
endmodule
