module reductions8(input [7:0] a,output all_set,any_set,parity,nand_bits,nor_bits,xnor_bits,is_zero,output [7:0] inverted);
  assign all_set = &a;
  assign any_set = |a;
  assign parity = ^a;
  assign nand_bits = ~&a;
  assign nor_bits = ~|a;
  assign xnor_bits = ~^a;
  assign is_zero = !a;
  assign inverted = ~a;
endmodule
