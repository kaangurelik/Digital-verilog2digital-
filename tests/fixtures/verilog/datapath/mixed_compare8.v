module mixed_compare8(input signed [7:0] a,input [7:0] b,output less,equal);
  assign less = a < b;
  assign equal = a == b;
endmodule
