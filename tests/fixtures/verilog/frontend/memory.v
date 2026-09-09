module memory(input [3:0] address, output [7:0] y);
  reg [7:0] data [0:15];
  assign y = data[address];
endmodule
