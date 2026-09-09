module logical_cases(input a, b, c, output both, either, inverted);
  assign both = a && b;
  assign either = a || c;
  assign inverted = !b;
endmodule
