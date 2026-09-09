module mux8(input [7:0] a,b,input select_b,output [7:0] y);
  assign y = select_b ? b : a;
endmodule
