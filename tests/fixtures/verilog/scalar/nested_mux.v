module nested_mux(input a, b, c, outer_select, inner_select, output y);
  assign y = outer_select ? (inner_select ? a : b) : c;
endmodule
