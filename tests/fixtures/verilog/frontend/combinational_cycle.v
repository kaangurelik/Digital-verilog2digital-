module combinational_cycle(output y);
  wire feedback;
  assign feedback = ~feedback;
  assign y = feedback;
endmodule
