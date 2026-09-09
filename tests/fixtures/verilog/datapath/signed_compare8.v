module signed_compare8(input signed [7:0] a,b, output less,less_equal,greater,greater_equal,equal,unequal);
  assign less = a < b;
  assign less_equal = a <= b;
  assign greater = a > b;
  assign greater_equal = a >= b;
  assign equal = a == b;
  assign unequal = a != b;
endmodule
