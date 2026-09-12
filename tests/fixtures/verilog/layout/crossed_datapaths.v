module crossed_datapaths(input [7:0] a,b,c,d,output [7:0] u,v);
  wire [7:0] p,q,r,s;
  assign p = a + d;
  assign q = b - c;
  assign r = a ^ c;
  assign s = b | d;
  assign u = p ^ q;
  assign v = r + s;
endmodule
