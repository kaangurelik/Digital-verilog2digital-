module macro_consumer(input [3:0] a, output [3:0] y);
  assign y = a ^ `SHARED_MASK;
endmodule
