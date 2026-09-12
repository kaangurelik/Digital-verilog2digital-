module chain_small(input a,b,output y);
  wire n0,n1,n2,n3,n4;
  assign n0 = a ^ b;
  assign n1 = n0 & a;
  assign n2 = n1 ^ b;
  assign n3 = n2 | a;
  assign n4 = ~n3;
  assign y = n4 ^ b;
endmodule
