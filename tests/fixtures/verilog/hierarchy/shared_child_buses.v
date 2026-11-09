module shared_child_buses(input [3:0] a,b,output [3:0] y,z);
hier_shared first(.a(a),.unused(b),.y(y),.same());
hier_shared second(.a(b),.unused(a),.y(),.same(z));
endmodule
module hier_shared(input [3:0] a,unused,output [3:0] y,same);
assign y=~a;
assign same=a;
endmodule
