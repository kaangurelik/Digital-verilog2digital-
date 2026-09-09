module synthesis_profile(input a, output y);
`ifdef SYNTHESIS
  `ifdef YOSYS
    assign y = ~a;
  `else
    initial $display("YOSYS profile definition missing");
    assign y = a;
  `endif
`else
  initial $display("SYNTHESIS profile definition missing");
  assign y = a;
`endif
endmodule
