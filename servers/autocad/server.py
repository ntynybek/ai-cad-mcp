from mcp.server.fastmcp import FastMCP
import win32com.client
import pythoncom

mcp = FastMCP("autocad-assistant")

def _point(x: float, y: float, z: float=0.0):
    """Create a 3D point."""
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [x, y, z])

@mcp.tool()
def ping() -> str:
    """Ping the AutoCAD assistant."""
    return "pong"

@mcp.tool()
def get_autocad_status() -> str:
    """Get the status of AutoCAD."""
    try:
        pythoncom.CoInitialize()
        acad = win32com.client.GetActiveObject("AutoCAD.Application")
        doc = acad.ActiveDocument
        return f"AutoCAD {acad.Version} is running. The active document is '{doc.Name}'."
    except pythoncom.com_error as e:
        return f"AutoCAD is not running: {e}"
    finally:
        pythoncom.CoUninitialize()

@mcp.tool()
def draw_line(x1: float, y1: float, z1: float, x2: float, y2: float, z2: float) -> str:
    """Draw a line in AutoCAD from (x1, y1, z1) to (x2, y2, z2)."""
    try:
        pythoncom.CoInitialize()

        acad = win32com.client.GetActiveObject("AutoCAD.Application")
        doc = acad.ActiveDocument
        model_space = doc.ModelSpace

        start_point = _point(x1, y1, z1)
        end_point = _point(x2, y2, z2)

        line = model_space.AddLine(start_point, end_point)
        doc.Regen(1)

        return f"Line drawn from ({x1}, {y1}, {z1}) to ({x2}, {y2}, {z2})."
    
    except pythoncom.com_error as e:
        return f"Failed to draw line: {e}"
    except Exception as e:
        return f"An unexpected error occurred: {e}"
    finally:
        pythoncom.CoUninitialize()

if __name__ == "__main__":
    mcp.run(transport="stdio")