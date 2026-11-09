module unused_child_input(input a,b,c,output y,z);
hier_unused_bit first(.a(a),.unused(b),.y(y));
hier_unused_outer second(.a(c),.unused(a),.y(z));
endmodule
module hier_unused_bit(input a,unused,output y);
assign y=~a;
endmodule
module hier_unused_outer(input a,unused,output y);
hier_unused_bit inner(.a(a),.unused(unused),.y(y));
endmodule
