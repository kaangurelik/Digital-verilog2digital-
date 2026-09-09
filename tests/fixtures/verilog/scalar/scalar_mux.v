module scalar_mux(input a, b, select_b, output y);
  assign y = select_b ? b : a;
endmodule
