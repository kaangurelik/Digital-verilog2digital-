module bus_alias(input [15:8] a, output [15:8] y, output [3:0] low);
  assign y = a;
  assign low = a[11:8];
endmodule
