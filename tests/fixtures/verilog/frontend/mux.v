module mux(input [3:0] a, input [3:0] b, input select_b, output [3:0] y);
  assign y = select_b ? b : a;
endmodule
