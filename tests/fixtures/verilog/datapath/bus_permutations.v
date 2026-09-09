module bus_permutations(input [7:0] a, output [7:0] reversed, swapped, repeated, mixed);
  assign reversed = {a[0],a[1],a[2],a[3],a[4],a[5],a[6],a[7]};
  assign swapped = {a[3:0],a[7:4]};
  assign repeated = {4{a[1:0]}};
  assign mixed = {2'b10,a[1:0],a[7:4]};
endmodule
