module shifts8(input [7:0] a,input [3:0] amount,output [7:0] left,right,arithmetic,signed_left);
  assign left = a << amount;
  assign right = a >> amount;
  assign arithmetic = $signed(a) >>> amount;
  assign signed_left = $signed(a) <<< amount;
endmodule
