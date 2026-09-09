module ascending(input [0:7] a, output [7:0] y, output lsb, output msb);
  assign y = a;
  assign lsb = a[7];
  assign msb = a[0];
endmodule
