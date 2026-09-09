`include "frontend_config.vh"
module include_top(input [`WORD_BITS-1:0] a, output [`WORD_BITS-1:0] y);
  assign y = a ^ `MASK;
endmodule
