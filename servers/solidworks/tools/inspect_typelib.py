"""Inspect the installed API without starting SOLIDWORKS."""
import sys
import pythoncom

lib = pythoncom.LoadTypeLib(sys.argv[1])
need = set(sys.argv[2:])
for i in range(lib.GetTypeInfoCount()):
    info = lib.GetTypeInfo(i)
    name = lib.GetDocumentation(i)[0]
    for j in range(info.GetTypeAttr().cFuncs):
        desc = info.GetFuncDesc(j)
        names = info.GetNames(desc.memid)
        if names[0] in need:
            print(name, names, "invkind", desc.invkind, "args", desc.args, "return", desc.rettype)
