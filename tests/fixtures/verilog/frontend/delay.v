module delay(input a, output y);
  assign #1 y = a;
endmodule
