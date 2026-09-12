module reconvergent(input a,b,c,d,output y,z);
  wire left,right,shared;
  assign left = a & b;
  assign right = c | d;
  assign shared = left ^ right;
  assign y = shared ? a : c;
  assign z = shared ? b : d;
endmodule
