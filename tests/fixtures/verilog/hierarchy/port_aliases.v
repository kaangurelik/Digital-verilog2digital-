module port_aliases(input [3:0] a,output [3:0] direct,reversed,repeated,zeros);
hier_alias child(.a(a),.direct(direct),.reversed(reversed),.repeated(repeated),.zeros(zeros));
endmodule
module hier_alias(input [3:0] a,output [3:0] direct,reversed,repeated,zeros);
assign direct=a;
assign reversed={a[0],a[1],a[2],a[3]};
assign repeated={a[3],a[3],a[0],a[0]};
assign zeros=4'd0;
endmodule
