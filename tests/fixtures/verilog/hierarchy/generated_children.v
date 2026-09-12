module generated_children(input [7:0] a,b,output [7:0] y);
genvar lane;
generate for(lane=0;lane<2;lane=lane+1) begin: lanes
  hier_lane \xor.child (.a(a[lane*4+:4]),.b(b[lane*4+:4]),.y(y[lane*4+:4]));
end endgenerate
endmodule
module hier_lane(input [3:0] a,b,output [3:0] y);
assign y=a^b;
endmodule
