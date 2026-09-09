module comparisons4(input [3:0] a,b, output equal,unequal,less,less_equal,greater,greater_equal,signed_less,signed_greater_equal);
  assign equal = a == b;
  assign unequal = a != b;
  assign less = a < b;
  assign less_equal = a <= b;
  assign greater = a > b;
  assign greater_equal = a >= b;
  assign signed_less = $signed(a) < $signed(b);
  assign signed_greater_equal = $signed(a) >= $signed(b);
endmodule
