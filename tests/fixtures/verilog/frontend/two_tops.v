module invert_top(input a, output y);
  assign y = ~a;
endmodule

module passthrough_top(input a, output y);
  assign y = a;
endmodule
