module shifts64(input [63:0] a,input [6:0] amount,output [63:0] left,right,arithmetic);
  assign left = a << amount;
  assign right = a >> amount;
  assign arithmetic = $signed(a) >>> amount;
endmodule
