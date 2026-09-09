module parameterized #(parameter WIDTH = 4, parameter BIAS = 3)
    (input [WIDTH-1:0] a, output [WIDTH-1:0] y);
  assign y = a + BIAS;
endmodule
