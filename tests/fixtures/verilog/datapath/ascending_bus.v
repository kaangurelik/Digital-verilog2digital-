module ascending_bus(input [0:7] a, output [7:0] y, swapped, output lsb, msb);
  assign y = a;
  assign swapped = {a[4:7],a[0:3]};
  assign lsb = a[7];
  assign msb = a[0];
endmodule
