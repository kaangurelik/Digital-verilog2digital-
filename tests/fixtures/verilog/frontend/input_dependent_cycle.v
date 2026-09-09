module input_dependent_cycle(input a, output y);
  wire feedback;
  assign feedback = feedback ^ a;
  assign y = feedback;
endmodule
