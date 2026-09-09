module fanout(input a, b, output y, z, direct);
  assign y = a & b;
  assign z = a ^ b;
  assign direct = a;
endmodule
