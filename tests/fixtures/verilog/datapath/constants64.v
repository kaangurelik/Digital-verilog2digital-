module constants64(output [63:0] high_and_low,ones,zero);
  assign high_and_low = 64'h8000000000000001;
  assign ones = 64'hffffffffffffffff;
  assign zero = 64'h0000000000000000;
endmodule
