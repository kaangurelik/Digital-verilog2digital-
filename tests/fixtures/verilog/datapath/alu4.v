module alu4(input [3:0] a,b,input [1:0] operation,output [3:0] y,output is_zero);
  assign y = operation == 2'b00 ? a+b :
             operation == 2'b01 ? a-b :
             operation == 2'b10 ? a^b : a&b;
  assign is_zero = y == 4'b0000;
endmodule
