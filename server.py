import sys 
import os 
from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types 
import win32com.client 
pythoncom = None

app = Server("autocad-mcp-server")

def get_autocad_app():
    """Get the AutoCAD application instance."""
    try:
        acad = win32com.client.Dispatch("AutoCAD.Application")
        acad.Visible = True
        return acad
    except Exception as e:
        raise RuntimeError("Failed to connect to AutoCAD: {}".format(e))    

@app.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="autocad_create_line",
            description="Create a line in AutoCAD",
            inputSchema={
                "type": "object",
                "properties": {
                    "x1": {"type": "number", "description": "X coordinate of the start point"},
                    "y1": {"type": "number", "description": "Y coordinate of the start point"},
                    "z1": {"type": "number", "description": "Z coordinate of the start point"},
                    "x2": {"type": "number", "description": "X coordinate of the end point"},
                    "y2": {"type": "number", "description": "Y coordinate of the end point"},
                    "z2": {"type": "number", "description": "Z coordinate of the end point"},
                },
                "required": ["x1", "y1", "z1", "x2", "y2", "z2"],
            },
        ),
        types.Tool(
            name="autocad_get_active_drawing_info",
            description="Get information about the active drawing in AutoCAD",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]

@app.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Processing tool calls from AI"""
    acad = get_autocad_app()
    doc = acad.ActiveDocument

    if name == "autocad_create_line":
        x1, y1, z1 = arguments["x1"], arguments["y1"], arguments["z1"]
        x2, y2, z2 = arguments["x2"], arguments["y2"], arguments["z2"]  

        pt1 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x1, y1, z1]) 
        pt2 = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x2, y2, z2])
  
        line = doc.ModelSpace.AddLine(pt1, pt2)
        doc.Regen(1)
        return [types.TextContent(type="text", text=f"Line created from ({x1}, {y1}, {z1}) to ({x2}, {y2}, {z2}). ID: {line.Handle}")]
    
    elif name == "autocad_get_active_drawing_info":
        name_file = doc.Name
        count = doc.ModelSpace.Count
        return [types.TextContent(type="text", text=f"Active drawing: {name_file}, Number of entities in ModelSpace: {count}")]

    raise ValueError(f"Unknown tool name: {name}")

async def main():
    """Main entry point for the server."""
    async with stdio_server(app) as (read_stream, write_stream):
        await app.run(
            read_stream, 
            write_stream, 
            app.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())   

