module repeated_operand(input a, output zero, direct);
  assign zero = a ^ a;
  assign direct = a & a;
endmodule
