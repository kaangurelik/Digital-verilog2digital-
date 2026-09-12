module mixed_symbols(input [3:0] a,b,input select_difference,output [4:0] y,output less);
  wire [4:0] sum,difference;
  assign sum = {1'b0,a} + {1'b0,b};
  assign difference = {1'b0,a} - {1'b0,b};
  assign y = select_difference ? difference : sum;
  assign less = a < b;
endmodule
