module chain_medium(input [7:0] a,b,output [7:0] y);
  wire [7:0] n0,n1,n2,n3,n4,n5,n6;
  assign n0 = a ^ b;
  assign n1 = n0 + a;
  assign n2 = n1 ^ b;
  assign n3 = n2 - a;
  assign n4 = n3 | b;
  assign n5 = n4 + a;
  assign n6 = n5 & b;
  assign y = n6 ^ a;
endmodule
