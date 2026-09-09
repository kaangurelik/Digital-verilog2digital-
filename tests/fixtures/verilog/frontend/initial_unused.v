module initial_unused(input a, output y);
  reg unused;
  initial unused = 1'b0;
  assign y = a;
endmodule
