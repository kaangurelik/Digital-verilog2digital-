module arithmetic64(input [63:0] a,b,output [63:0] sum,difference,output signed_less,unsigned_less);
  assign sum = a + b;
  assign difference = a - b;
  assign signed_less = $signed(a) < $signed(b);
  assign unsigned_less = a < b;
endmodule
