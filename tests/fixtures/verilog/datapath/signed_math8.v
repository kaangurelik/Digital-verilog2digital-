module signed_math8(input signed [7:0] a,b, output signed [8:0] sum,difference);
  assign sum = a + b;
  assign difference = a - b;
endmodule
